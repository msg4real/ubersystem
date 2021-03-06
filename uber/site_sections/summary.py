import os
import re
from collections import defaultdict, OrderedDict
from datetime import datetime, timedelta

import cherrypy
import six
from dateutil.relativedelta import relativedelta
from pytz import UTC
from sqlalchemy import and_, or_, func
from sqlalchemy.sql.expression import extract, literal
from sqlalchemy.orm import joinedload, subqueryload

from uber.config import c
from uber.decorators import all_renderable, csv_file, multifile_zipfile, render, site_mappable, xlsx_file, \
    _set_response_filename
from uber.errors import HTTPRedirect
from uber.models import Attendee, Department, Event, FoodRestrictions, Group, Session
from uber.reports import PersonalizedBadgeReport, PrintedBadgeReport
from uber.utils import filename_safe, localized_now


def generate_staff_badges(start_badge, end_badge, out, session):
    assert start_badge >= c.BADGE_RANGES[c.STAFF_BADGE][0]
    assert end_badge <= c.BADGE_RANGES[c.STAFF_BADGE][1]

    badge_range = (start_badge, end_badge)

    PrintedBadgeReport(
        badge_type=c.STAFF_BADGE,
        range=badge_range,
        badge_type_name='Staff').run(out, session)


def volunteer_checklists(session):
    attendees = session.query(Attendee) \
        .filter(
            Attendee.staffing == True,
            Attendee.badge_status.in_([c.NEW_STATUS, c.COMPLETED_STATUS])) \
        .order_by(Attendee.full_name, Attendee.id).all()  # noqa: E712

    checklist_items = OrderedDict()
    for item_template in c.VOLUNTEER_CHECKLIST:
        item_name = os.path.splitext(os.path.basename(item_template))[0]
        if item_name.endswith('_item'):
            item_name = item_name[:-5]
        item_name = item_name.replace('_', ' ').title()
        checklist_items[item_name] = item_template

    re_checkbox = re.compile(r'<img src="\.\./static/images/checkbox_.*?/>')
    for attendee in attendees:
        attendee.checklist_items = OrderedDict()
        for item_name, item_template in checklist_items.items():
            html = render(item_template, {'attendee': attendee}, encoding=None)
            match = re_checkbox.search(html)
            is_complete = False
            is_applicable = False
            if match:
                is_applicable = True
                checkbox_html = match.group(0)
                if 'checkbox_checked' in checkbox_html:
                    is_complete = True
            attendee.checklist_items[item_name] = {
                'is_applicable': is_applicable,
                'is_complete': is_complete,
            }

    return {
        'checklist_items': checklist_items,
        'attendees': attendees,
    }


def _get_guidebook_models(session, selected_model=''):
    model = selected_model.split('_')[0] if '_' in selected_model else selected_model
    model_query = session.query(Session.resolve_model(model))

    if '_band' in selected_model:
        return model_query.filter_by(group_type=c.BAND)
    elif '_guest' in selected_model:
        return model_query.filter_by(group_type=c.GUEST)
    elif '_dealer' in selected_model:
        return model_query.filter_by(is_dealer=True)
    elif '_panels' in selected_model:
        return model_query.filter(Event.location.in_(c.PANEL_ROOMS))
    elif 'Game' in selected_model:
        return model_query.filter_by(has_been_accepted=True)
    else:
        return model_query


@all_renderable(c.STATS)
class Root:
    def index(self, session):
        counts = defaultdict(OrderedDict)
        counts['donation_tiers'] = OrderedDict([(k, 0) for k in sorted(c.DONATION_TIERS.keys()) if k > 0])

        counts.update({
            'groups': {'paid': 0, 'free': 0},
            'noshows': {'paid': 0, 'free': 0},
            'checked_in': {'yes': 0, 'no': 0}
        })
        count_labels = {
            'badges': c.BADGE_OPTS,
            'paid': c.PAYMENT_OPTS,
            'ages': c.AGE_GROUP_OPTS,
            'ribbons': c.RIBBON_OPTS,
            'interests': c.INTEREST_OPTS,
            'statuses': c.BADGE_STATUS_OPTS,
            'checked_in_by_type': c.BADGE_OPTS,
        }
        for label, opts in count_labels.items():
            for val, desc in opts:
                counts[label][desc] = 0
        stocks = c.BADGE_PRICES['stocks']
        for var in c.BADGE_VARS:
            badge_type = getattr(c, var)
            counts['stocks'][c.BADGES[badge_type]] = stocks.get(var.lower(), 'no limit set')
            counts['counts'][c.BADGES[badge_type]] = c.get_badge_count_by_type(badge_type)

        for a in session.query(Attendee).options(joinedload(Attendee.group)):
            counts['paid'][a.paid_label] += 1
            counts['ages'][a.age_group_label] += 1
            for val in a.ribbon_ints:
                counts['ribbons'][c.RIBBONS[val]] += 1
            counts['badges'][a.badge_type_label] += 1
            counts['statuses'][a.badge_status_label] += 1
            counts['checked_in']['yes' if a.checked_in else 'no'] += 1
            if a.checked_in:
                counts['checked_in_by_type'][a.badge_type_label] += 1
            for val in a.interests_ints:
                counts['interests'][c.INTERESTS[val]] += 1
            if a.paid == c.PAID_BY_GROUP and a.group:
                counts['groups']['paid' if a.group.amount_paid else 'free'] += 1

            donation_amounts = list(counts['donation_tiers'].keys())
            for index, amount in enumerate(donation_amounts):
                next_amount = donation_amounts[index + 1] if index + 1 < len(donation_amounts) else six.MAXSIZE
                if a.amount_extra >= amount and a.amount_extra < next_amount:
                    counts['donation_tiers'][amount] = counts['donation_tiers'][amount] + 1
            if not a.checked_in:
                is_paid = a.paid == c.HAS_PAID or a.paid == c.PAID_BY_GROUP and a.group and a.group.amount_paid
                key = 'paid' if is_paid else 'free'
                counts['noshows'][key] += 1

        return {
            'counts': counts,
            'total_registrations': session.query(Attendee).count()
        }

    def affiliates(self, session):
        class AffiliateCounts:
            def __init__(self):
                self.tally, self.total = 0, 0
                self.amounts = {}

            @property
            def sorted(self):
                return sorted(self.amounts.items())

            def count(self, amount):
                self.tally += 1
                self.total += amount
                self.amounts[amount] = 1 + self.amounts.get(amount, 0)

        counts = defaultdict(AffiliateCounts)
        for affiliate, amount in (session.query(Attendee.affiliate, Attendee.amount_extra)
                                         .filter(Attendee.amount_extra > 0)):
            counts['everything combined'].count(amount)
            counts[affiliate or 'no affiliate selected'].count(amount)

        return {
            'counts': sorted(counts.items(), key=lambda tup: -tup[-1].total),
            'registrations': session.query(Attendee).filter_by(paid=c.NEED_NOT_PAY).count(),
            'quantities': [(desc, session.query(Attendee).filter(Attendee.amount_extra >= amount).count())
                           for amount, desc in sorted(c.DONATION_TIERS.items()) if amount]
        }

    def departments(self, session):
        everything = []
        departments = session.query(Department).options(
            subqueryload(Department.members).subqueryload(Attendee.dept_memberships),
            subqueryload(Department.unassigned_explicitly_requesting_attendees)).order_by(Department.name)
        for department in departments:
            assigned = department.members
            unassigned = department.unassigned_explicitly_requesting_attendees
            everything.append([department, assigned, unassigned])
        return {'everything': everything}

    def found_how(self, session):
        return {'all': sorted(
            [a.found_how for a in session.query(Attendee).filter(Attendee.found_how != '').all()],
            key=lambda s: s.lower())}

    def all_schedules(self, session):
        return {'staffers': [a for a in session.staffers() if a.shifts]}

    def food_restrictions(self, session):
        all_fr = session.query(FoodRestrictions).all()
        guests = session.query(Attendee).filter_by(badge_type=c.GUEST_BADGE).count()
        volunteers = len([
            a for a in session.query(Attendee).filter_by(staffing=True).all()
            if a.badge_type == c.STAFF_BADGE or a.weighted_hours or not a.takes_shifts])

        return {
            'guests': guests,
            'volunteers': volunteers,
            'notes': filter(bool, [getattr(fr, 'freeform', '') for fr in all_fr]),
            'standard': {
                c.FOOD_RESTRICTIONS[getattr(c, category)]: len([fr for fr in all_fr if getattr(fr, category)])
                for category in c.FOOD_RESTRICTION_VARS
            },
            'sandwich_prefs': {
                desc: len([fr for fr in all_fr if val in fr.sandwich_pref_ints])
                for val, desc in c.SANDWICH_OPTS
            }
        }

    def ratings(self, session):
        return {
            'prev_years': [a for a in session.staffers() if 'poorly' in a.past_years],
            'current': [a for a in session.staffers() if any(shift.rating == c.RATED_BAD for shift in a.shifts)]
        }

    def staffing_overview(self, session):
        attendees = session.staffers().options(subqueryload(Attendee.dept_memberships)).all()
        attendees_by_dept = defaultdict(list)
        for attendee in attendees:
            for dept_membership in attendee.dept_memberships:
                attendees_by_dept[dept_membership.department_id].append(attendee)

        jobs = session.jobs().all()
        jobs_by_dept = defaultdict(list)
        for job in jobs:
            jobs_by_dept[job.department_id].append(job)

        departments = session.query(Department).order_by(Department.name)

        return {
            'hour_total': sum(j.weighted_hours * j.slots for j in jobs),
            'shift_total': sum(j.weighted_hours * len(j.shifts) for j in jobs),
            'volunteers': len(attendees),
            'departments': [{
                'department': dept,
                'assigned': len(attendees_by_dept[dept.id]),
                'total_hours': sum(j.weighted_hours * j.slots for j in jobs_by_dept[dept.id]),
                'taken_hours': sum(j.weighted_hours * len(j.shifts) for j in jobs_by_dept[dept.id])
            } for dept in departments]
        }

    def volunteer_hours_overview(self, session, message=''):
        attendees = session.staffers()
        return {
            'volunteers': attendees,
            'message': message,
        }

    @csv_file
    def dept_head_contact_info(self, out, session):
        out.writerow(["Full Name", "Email", "Phone", "Department(s)"])
        for a in session.query(Attendee).filter(Attendee.dept_memberships_as_dept_head.any()).order_by('last_name'):
            for label in a.assigned_depts_labels:
                out.writerow([a.full_name, a.email, a.cellphone, label])

    @csv_file
    def dealer_table_info(self, out, session):
        out.writerow([
            'Business Name',
            'Description',
            'URL',
            'Point of Contact',
            'Email',
            'Phone Number',
            'Address1',
            'Address2',
            'City',
            'State/Region',
            'Zip Code',
            'Country',
            'Tables',
            'Amount Paid',
            'Cost',
            'Badges'
        ])
        dealer_groups = session.query(Group).filter(Group.tables > 0).all()
        for group in dealer_groups:
            if group.approved and group.is_dealer:
                out.writerow([
                    group.name,
                    group.description,
                    group.website,
                    group.leader.legal_name or group.leader.full_name,
                    group.leader.email,
                    group.leader.cellphone,
                    group.address1,
                    group.address2,
                    group.city,
                    group.region,
                    group.zip_code,
                    group.country,
                    group.tables,
                    group.amount_paid,
                    group.cost,
                    group.badges
                ])

    @xlsx_file
    def vendor_comptroller_info(self, out, session):
        dealer_groups = session.query(Group).filter(Group.tables > 0).all()
        rows = []
        for group in dealer_groups:
            if group.approved and group.is_dealer:
                rows.append([
                    group.name,
                    group.leader.email,
                    group.leader.legal_name or group.leader.full_name,
                    group.leader.cellphone,
                    group.physical_address
                ])
        header_row = [
            'Vendor Name',
            'Contact Email',
            'Primary Contact',
            'Contact Phone #',
            'Physical Address']
        out.writerows(header_row, rows)

    @xlsx_file
    def printed_badges_attendee(self, out, session):
        PrintedBadgeReport(badge_type=c.ATTENDEE_BADGE, badge_type_name='Attendee').run(out, session)

    @xlsx_file
    def printed_badges_guest(self, out, session):
        PrintedBadgeReport(badge_type=c.GUEST_BADGE, badge_type_name='Guest').run(out, session)

    @xlsx_file
    def printed_badges_one_day(self, out, session):
        PrintedBadgeReport(badge_type=c.ONE_DAY_BADGE, badge_type_name='OneDay').run(out, session)

    @xlsx_file
    def printed_badges_minor(self, out, session):
        try:
            PrintedBadgeReport(badge_type=c.CHILD_BADGE, badge_type_name='Minor').run(out, session)
        except AttributeError:
            pass

    @xlsx_file
    def printed_badges_staff(self, out, session):

        # part 1, include only staff badges that have an assigned name
        PersonalizedBadgeReport().run(
            out,
            session,
            Attendee.badge_type == c.STAFF_BADGE,
            Attendee.badge_num != None,
            order_by='badge_num')  # noqa: E711

        # part 2, include some extra for safety marging
        minimum_extra_amount = 5

        max_badges = c.BADGE_RANGES[c.STAFF_BADGE][1]
        start_badge = max_badges - minimum_extra_amount + 1
        end_badge = max_badges

        generate_staff_badges(start_badge, end_badge, out, session)

    @xlsx_file
    def printed_badges_staff__expert_mode_only(self, out, session, start_badge, end_badge):
        """
        Generate a CSV of staff badges. Note: This is not normally what you would call to do the badge export.
        For use by experts only.
        """

        generate_staff_badges(int(start_badge), int(end_badge), out, session)

    @xlsx_file
    def badge_hangars_supporters(self, out, session):
        PersonalizedBadgeReport(include_badge_nums=False).run(
            out,
            session,
            Attendee.amount_extra >= c.SUPPORTER_LEVEL,
            order_by=Attendee.full_name,
            badge_type_override=lambda a: 'Super Supporter' if a.amount_extra >= c.SEASON_LEVEL else 'Supporter')

    """
    Enumerate individual CSVs here that will be integrated into the .zip which will contain all the
    badge types.  Downstream plugins can override which items are in this list.
    """
    badge_zipfile_contents = [
        printed_badges_attendee,
        printed_badges_guest,
        printed_badges_one_day,
        printed_badges_minor,
        printed_badges_staff,
        badge_hangars_supporters,
    ]

    @multifile_zipfile
    def personalized_badges_zip(self, zip_file, session):
        """
        Put all printed badge report files in one convenient zipfile.  The idea
        is that this ZIP file, unmodified, should be completely ready to send to
        the badge printers.

        Plugins can override badge_zipfile_contents to do something different/event-specific.
        """
        for badge_report_fn in self.badge_zipfile_contents:
            # run the report function, but don't output headers because
            # 1) we'll do it with the zipfile
            # 2) we don't set headers until the very end when everything is 100% good
            #    so that exceptions are displayed to the end user properly
            output = badge_report_fn(self, session, set_headers=False)

            filename = '{}.{}'.format(badge_report_fn.__name__, badge_report_fn.output_file_extension or '')
            zip_file.writestr(filename, output)

    def food_eligible(self, session):
        cherrypy.response.headers['Content-Type'] = 'application/xml'
        eligible = {
            a: {attr.lower(): getattr(a.food_restrictions, attr, False) for attr in c.FOOD_RESTRICTION_VARS}
            for a in session.staffers().all() + session.query(Attendee).filter_by(badge_type=c.GUEST_BADGE).all()
            if not a.is_unassigned and (
                a.badge_type in (c.STAFF_BADGE, c.GUEST_BADGE)
                or c.VOLUNTEER_RIBBON in a.ribbon_ints
                and a.weighted_hours >= 12)
        }
        return render('summary/food_eligible.xml', {'attendees': eligible})

    @csv_file
    def volunteers_with_worked_hours(self, out, session):
        out.writerow(['Badge #', 'Full Name', 'E-mail Address', 'Weighted Hours Scheduled', 'Weighted Hours Worked'])
        for a in session.query(Attendee).all():
            if a.worked_hours > 0:
                out.writerow([a.badge_num, a.full_name, a.email, a.weighted_hours, a.worked_hours])

    def shirt_manufacturing_counts(self, session):
        """
        This report should be the definitive report about the count and sizes of
        shirts needed to be ordered.

        There are two types of shirts:
        - "staff shirts" - staff uniforms, each staff gets c.SHIRTS_PER_STAFFER
        - "event shirts" - pre-ordered swag shirts, which are received by:
            - volunteers (non-staff who get one for free)
            - attendees (who can pre-order them)
        """
        counts = defaultdict(lambda: defaultdict(int))
        labels = ['size unknown'] + [label for val, label in c.SHIRT_OPTS][1:]

        def sort(d):
            return sorted(d.items(), key=lambda tup: labels.index(tup[0]))

        def label(s):
            return 'size unknown' if s == c.SHIRTS[c.NO_SHIRT] else s

        for attendee in session.all_attendees():
            shirt_label = attendee.shirt_label or 'size unknown'
            counts['staff'][label(shirt_label)] += attendee.num_staff_shirts_owed
            counts['event'][label(shirt_label)] += attendee.num_event_shirts_owed

        categories = []
        if c.SHIRTS_PER_STAFFER > 0:
            categories.append(('Staff Uniform Shirts', sort(counts['staff'])))

        categories.append(('Event Shirts', sort(counts['event'])))

        return {
            'categories': categories,
        }

    def shirt_counts(self, session):
        counts = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
        labels = ['size unknown'] + [label for val, label in c.SHIRT_OPTS][1:]

        def sort(d):
            return sorted(d.items(), key=lambda tup: labels.index(tup[0]))

        def label(s):
            return 'size unknown' if s == c.SHIRTS[c.NO_SHIRT] else s

        def status(got_merch):
            return 'picked_up' if got_merch else 'outstanding'

        sales_by_week = OrderedDict([(i, 0) for i in range(50)])

        for attendee in session.all_attendees():
            shirt_label = attendee.shirt_label or 'size unknown'
            counts['all_staff_shirts'][label(shirt_label)][status(attendee.got_merch)] += attendee.num_staff_shirts_owed
            counts['all_event_shirts'][label(shirt_label)][status(attendee.got_merch)] += attendee.num_event_shirts_owed
            if attendee.volunteer_event_shirt_eligible or attendee.replacement_staff_shirts:
                counts['free_event_shirts'][label(shirt_label)][status(attendee.got_merch)] += 1
            if attendee.paid_for_a_shirt:
                counts['paid_event_shirts'][label(shirt_label)][status(attendee.got_merch)] += 1
                sales_by_week[(min(datetime.now(UTC), c.ESCHATON) - attendee.registered).days // 7] += 1

        for week in range(48, -1, -1):
            sales_by_week[week] += sales_by_week[week + 1]

        categories = [
            ('Free Event Shirts', sort(counts['free_event_shirts'])),
            ('Paid Event Shirts', sort(counts['paid_event_shirts'])),
            ('All Event Shirts', sort(counts['all_event_shirts'])),
        ]
        if c.SHIRTS_PER_STAFFER > 0:
            categories.append(('Staff Shirts', sort(counts['all_staff_shirts'])))

        return {
            'sales_by_week': sales_by_week,
            'categories': categories,
        }

    def extra_merch(self, session):
        return {
            'attendees': session.query(Attendee).filter(Attendee.extra_merch != '').order_by(Attendee.full_name).all()}

    def restricted_untaken(self, session):
        untaken = defaultdict(lambda: defaultdict(list))
        for job in session.jobs():
            if job.restricted and job.slots_taken < job.slots:
                for hour in job.hours:
                    untaken[job.department_id][hour].append(job)
        flagged = []
        for attendee in session.staffers():
            if not attendee.is_dept_head:
                overlapping = defaultdict(set)
                for shift in attendee.shifts:
                    if not shift.job.restricted:
                        for dept in attendee.assigned_depts:
                            for hour in shift.job.hours:
                                if attendee.trusted_in(dept) and hour in untaken[dept]:
                                    overlapping[shift.job].update(untaken[dept][hour])
                if overlapping:
                    flagged.append([attendee, sorted(overlapping.items(), key=lambda tup: tup[0].start_time)])
        return {'flagged': flagged}

    def consecutive_threshold(self, session):
        def exceeds_threshold(start_time, attendee):
            time_slice = [start_time + timedelta(hours=i) for i in range(18)]
            return len([h for h in attendee.hours if h in time_slice]) >= 12
        flagged = []
        for attendee in session.staffers():
            if attendee.staffing and attendee.weighted_hours >= 12:
                for start_time, desc in c.START_TIME_OPTS[::6]:
                    if exceeds_threshold(start_time, attendee):
                        flagged.append(attendee)
                        break
        return {'flagged': flagged}

    def setup_teardown_neglect(self, session):
        jobs = session.jobs().all()
        return {
            'unfilled': [
                ('Setup', [job for job in jobs if job.is_setup and job.slots_untaken]),
                ('Teardown', [job for job in jobs if job.is_teardown and job.slots_untaken])
            ]
        }

    def volunteers_owed_refunds(self, session):
        attendees = session.all_attendees().filter(Attendee.paid.in_([c.HAS_PAID, c.PAID_BY_GROUP, c.REFUNDED])).all()

        def is_unrefunded(a):
            return a.paid == c.HAS_PAID \
                or a.paid == c.PAID_BY_GROUP \
                and a.group \
                and a.group.amount_paid \
                and not a.group.amount_refunded

        return {
            'attendees': [(
                'Volunteers Owed Refunds',
                [a for a in attendees if is_unrefunded(a) and a.worked_hours >= c.HOURS_FOR_REFUND]
            ), (
                'Volunteers Already Refunded',
                [a for a in attendees if not is_unrefunded(a) and a.staffing]
            ), (
                'Volunteers Who Can Be Refunded Once Their Shifts Are Marked',
                [a for a in attendees if is_unrefunded(a)
                    and a.worked_hours < c.HOURS_FOR_REFUND and a.weighted_hours >= c.HOURS_FOR_REFUND]
            )]
        }

    @csv_file
    def volunteer_checklist_csv(self, out, session):
        checklists = volunteer_checklists(session)
        out.writerow(['First Name', 'Last Name', 'Email', 'Cellphone', 'Assigned Depts']
                     + [s for s in checklists['checklist_items'].keys()])
        for attendee in checklists['attendees']:
            checklist_items = []
            for item in attendee.checklist_items.values():
                checklist_items.append('Yes' if item['is_complete'] else 'No' if item['is_applicable'] else 'N/A')
            out.writerow([attendee.first_name,
                          attendee.last_name,
                          attendee.email,
                          attendee.cellphone,
                          ', '.join(attendee.assigned_depts_labels)
                          ] + checklist_items)

    def volunteer_checklists(self, session):
        return volunteer_checklists(session)

    @csv_file
    @site_mappable
    def requested_accessibility_services(self, out, session):
        out.writerow(['Badge #', 'Full Name', 'Badge Type', 'Email', 'Comments'])
        query = session.query(Attendee).filter_by(requested_accessibility_services=True)
        for person in query.all():
            out.writerow([
                person.badge_num, person.full_name, person.badge_type_label,
                person.email, person.comments
            ])

    requested_accessibility_services.restricted = [c.ACCESSIBILITY]

    @csv_file
    @site_mappable
    def attendee_birthday_calendar(
            self,
            out,
            session,
            year=datetime.now(UTC).year):

        out.writerow([
            'Subject', 'Start Date', 'Start Time', 'End Date', 'End Time',
            'All Day Event', 'Description', 'Location', 'Private'])

        query = session.query(Attendee).filter(Attendee.birthdate != None)  # noqa: E711
        for person in query.all():
            subject = "%s's Birthday" % person.full_name
            delta_years = year - person.birthdate.year
            start_date = person.birthdate + relativedelta(years=delta_years)
            end_date = start_date
            all_day = True
            private = False
            out.writerow([
                subject, start_date, '', end_date, '', all_day, '', '', private
            ])

    @csv_file
    @site_mappable
    def event_birthday_calendar(self, out, session):
        out.writerow([
            'Subject', 'Start Date', 'Start Time', 'End Date', 'End Time',
            'All Day Event', 'Description', 'Location', 'Private'])

        is_multiyear = c.EPOCH.year != c.ESCHATON.year
        is_multimonth = c.EPOCH.month != c.ESCHATON.month
        query = session.query(Attendee).filter(Attendee.birthdate != None)  # noqa: E711
        birth_month = extract('month', Attendee.birthdate)
        birth_day = extract('day', Attendee.birthdate)
        if is_multiyear:
            # The event starts in one year and ends in another
            query = query.filter(or_(
                or_(
                    birth_month > c.EPOCH.month,
                    birth_month < c.ESCHATON.month),
                and_(
                    birth_month == c.EPOCH.month,
                    birth_day >= c.EPOCH.day),
                and_(
                    birth_month == c.ESCHATON.month,
                    birth_day <= c.ESCHATON.day)))
        elif is_multimonth:
            # The event starts in one month and ends in another
            query = query.filter(or_(
                and_(
                    birth_month > c.EPOCH.month,
                    birth_month < c.ESCHATON.month),
                and_(
                    birth_month == c.EPOCH.month,
                    birth_day >= c.EPOCH.day),
                and_(
                    birth_month == c.ESCHATON.month,
                    birth_day <= c.ESCHATON.day)))
        else:
            # The event happens entirely within a single month
            query = query.filter(and_(
                birth_month == c.EPOCH.month,
                birth_day >= c.EPOCH.day,
                birth_day <= c.ESCHATON.day))

        for person in query.all():
            subject = "%s's Birthday" % person.full_name

            year_of_birthday = c.ESCHATON.year
            if is_multiyear:
                birth_month = person.birthdate.month
                birth_day = person.birthdate.day
                if birth_month >= c.EPOCH.month and birth_day >= c.EPOCH.day:
                    year_of_birthday = c.EPOCH.year

            delta_years = year_of_birthday - person.birthdate.year
            start_date = person.birthdate + relativedelta(years=delta_years)
            end_date = start_date
            all_day = True
            private = False
            out.writerow([
                subject, start_date, '', end_date, '', all_day, '', '', private
            ])

    @csv_file
    def checkins_by_hour(self, out, session):
        def date_trunc_hour(*args, **kwargs):
            # sqlite doesn't support date_trunc
            if c.SQLALCHEMY_URL.startswith('sqlite'):
                return func.strftime(literal('%Y-%m-%d %H:00'), *args, **kwargs)
            else:
                return func.date_trunc(literal('hour'), *args, **kwargs)

        out.writerow(["time_utc", "count"])
        query_result = session.query(
                date_trunc_hour(Attendee.checked_in),
                func.count(date_trunc_hour(Attendee.checked_in))
            ) \
            .filter(Attendee.checked_in.isnot(None)) \
            .group_by(date_trunc_hour(Attendee.checked_in)) \
            .order_by(date_trunc_hour(Attendee.checked_in)) \
            .all()

        for result in query_result:
            hour = result[0]
            count = result[1]
            out.writerow([hour, count])

    def all_attendees(self):
        raise HTTPRedirect('../export/valid_attendees')
    all_attendees.restricted = [c.ACCOUNTS and c.STATS and c.PEOPLE and c.MONEY]

    def guidebook_exports(self, session, message=''):
        return {
            'message': message,
            'tables': c.GUIDEBOOK_MODELS,
        }

    @xlsx_file
    def export_guidebook_xlsx(self, out, session, selected_model=''):
        model_list = _get_guidebook_models(session, selected_model).all()

        _set_response_filename('{}_guidebook_{}.xlsx'.format(
            filename_safe(dict(c.GUIDEBOOK_MODELS)[selected_model]).lower(),
            localized_now().strftime('%Y%m%d'),

        ))

        out.writerow([val for key, val in c.GUIDEBOOK_PROPERTIES])

        for model in model_list:
            row = []
            for key, val in c.GUIDEBOOK_PROPERTIES:
                row.append(getattr(model, key, '').replace('\n', '<br/>'))
            out.writerow(row)

    @multifile_zipfile
    def export_guidebook_zip(self, zip_file, session, selected_model=''):
        model_list = _get_guidebook_models(session, selected_model).all()

        for model in model_list:
            filenames, files = getattr(model, 'guidebook_images', ['', ''])

            for filename, file in zip(filenames, files):
                if filename:
                    zip_file.write(getattr(file, 'filepath', file.pic_fpath), filename)
