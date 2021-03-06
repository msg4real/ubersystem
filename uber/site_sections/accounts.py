import inspect
import uuid
from collections import defaultdict

import bcrypt
import cherrypy
from pockets import unwrap
from sqlalchemy.orm import subqueryload
from sqlalchemy.orm.exc import NoResultFound

from uber.config import c
from uber.decorators import (ajax, all_renderable, csrf_protected, csv_file,
                             department_id_adapter, render, site_mappable, unrestricted)
from uber.errors import HTTPRedirect
from uber.models import AdminAccount, Attendee, PasswordReset
from uber.tasks.email import send_email
from uber.utils import check, check_csrf, create_valid_user_supplied_redirect_url, ensure_csrf_token_exists, genpasswd


def valid_password(password, account):
    pr = account.password_reset
    if pr and pr.is_expired:
        account.session.delete(pr)
        pr = None

    all_hashed = [account.hashed] + ([pr.hashed] if pr else [])
    return any(bcrypt.hashpw(password, hashed) == hashed for hashed in all_hashed)


@all_renderable(c.ACCOUNTS)
class Root:
    def index(self, session, message=''):
        attendee_attrs = session.query(Attendee.id, Attendee.last_first, Attendee.badge_type, Attendee.badge_num) \
            .filter(Attendee.first_name != '', Attendee.badge_status not in [c.INVALID_STATUS, c.WATCHED_STATUS])

        attendees = [
            (id, '{} - {}{}'.format(name.title(), c.BADGES[badge_type], ' #{}'.format(badge_num) if badge_num else ''))
            for id, name, badge_type, badge_num in attendee_attrs]

        return {
            'message':  message,
            'accounts': (session.query(AdminAccount)
                         .join(Attendee)
                         .options(subqueryload(AdminAccount.attendee).subqueryload(Attendee.assigned_depts))
                         .order_by(Attendee.last_first).all()),
            'all_attendees': sorted(attendees, key=lambda tup: tup[1])
        }

    @csrf_protected
    def update(self, session, password='', message='', **params):
        account = session.admin_account(params, checkgroups=['access'])
        if account.is_new:
            if c.AT_OR_POST_CON and not password:
                message = 'You must enter a password'
            else:
                password = password if c.AT_OR_POST_CON else genpasswd()
                account.hashed = bcrypt.hashpw(password, bcrypt.gensalt())

        message = message or check(account)
        if not message:
            message = 'Account settings uploaded'
            attendee = session.attendee(account.attendee_id)  # dumb temporary hack, will fix later with tests
            account.attendee = attendee
            session.add(account)
            if account.is_new and not c.AT_OR_POST_CON:
                body = render('emails/accounts/new_account.txt', {
                    'account': account,
                    'password': password,
                    'creator': AdminAccount.admin_name()
                }, encoding=None)
                send_email.delay(
                    c.ADMIN_EMAIL,
                    attendee.email,
                    'New ' + c.EVENT_NAME + ' Ubersystem Account',
                    body,
                    model=attendee.to_dict('id'))
        else:
            session.rollback()

        raise HTTPRedirect('index?message={}', message)

    @csrf_protected
    def delete(self, session, id, **params):
        session.delete(session.admin_account(id))
        raise HTTPRedirect('index?message={}', 'Account deleted')

    @site_mappable
    @department_id_adapter
    def bulk(self, session, department_id=None, **params):
        department_id = None if department_id == 'All' else department_id
        attendee_filters = [Attendee.dept_memberships.any(department_id=department_id)] if department_id else []
        attendees = session.staffers().filter(*attendee_filters).all()
        for attendee in attendees:
            attendee.trusted_here = attendee.trusted_in(department_id) if department_id else attendee.has_role_somewhere
            attendee.hours_here = attendee.weighted_hours_in(department_id)

        return {
            'department_id':  department_id,
            'attendees': attendees
        }

    @unrestricted
    def login(self, session, message='', original_location=None, **params):
        original_location = create_valid_user_supplied_redirect_url(original_location, default_url='homepage')

        if 'email' in params:
            try:
                account = session.get_account_by_email(params['email'])
                if not valid_password(params['password'], account):
                    message = 'Incorrect password'
            except NoResultFound:
                message = 'No account exists for that email address'

            if not message:
                cherrypy.session['account_id'] = account.id
                ensure_csrf_token_exists()
                raise HTTPRedirect(original_location)

        return {
            'message': message,
            'email':   params.get('email', ''),
            'original_location': original_location,
        }

    @unrestricted
    def homepage(self, message=''):
        if not cherrypy.session.get('account_id'):
            raise HTTPRedirect('login?message={}', 'You are not logged in')
        return {'message': message}

    @unrestricted
    def logout(self):
        for key in list(cherrypy.session.keys()):
            if key not in ['preregs', 'paid_preregs', 'job_defaults', 'prev_location']:
                cherrypy.session.pop(key)
        raise HTTPRedirect('login?message={}', 'You have been logged out')

    @unrestricted
    def reset(self, session, message='', email=None):
        if email is not None:
            try:
                account = session.get_account_by_email(email)
            except NoResultFound:
                message = 'No account exists for email address {!r}'.format(email)
            else:
                password = genpasswd()
                if account.password_reset:
                    session.delete(account.password_reset)
                    session.commit()
                session.add(PasswordReset(admin_account=account, hashed=bcrypt.hashpw(password, bcrypt.gensalt())))
                body = render('emails/accounts/password_reset.txt', {
                    'name': account.attendee.full_name,
                    'password':  password}, encoding=None)

                send_email.delay(
                    c.ADMIN_EMAIL,
                    account.attendee.email,
                    c.EVENT_NAME + ' Admin Password Reset',
                    body,
                    model=account.attendee.to_dict('id'))
                raise HTTPRedirect('login?message={}', 'Your new password has been emailed to you')

        return {
            'email':   email,
            'message': message
        }

    def update_password_of_other(
            self,
            session,
            id,
            message='',
            updater_password=None,
            new_password=None,
            csrf_token=None,
            confirm_new_password=None):

        if updater_password is not None:
            new_password = new_password.strip()
            updater_account = session.admin_account(cherrypy.session['account_id'])
            if not new_password:
                message = 'New password is required'
            elif not valid_password(updater_password, updater_account):
                message = 'Your password is incorrect'
            elif new_password != confirm_new_password:
                message = 'Passwords do not match'
            else:
                check_csrf(csrf_token)
                account = session.admin_account(id)
                account.hashed = bcrypt.hashpw(new_password, bcrypt.gensalt())
                raise HTTPRedirect('index?message={}', 'Account Password Updated')

        return {
            'account': session.admin_account(id),
            'message': message
        }

    @unrestricted
    def change_password(
            self,
            session,
            message='',
            old_password=None,
            new_password=None,
            csrf_token=None,
            confirm_new_password=None):

        if not cherrypy.session.get('account_id'):
            raise HTTPRedirect('login?message={}', 'You are not logged in')

        if old_password is not None:
            new_password = new_password.strip()
            account = session.admin_account(cherrypy.session['account_id'])
            if not new_password:
                message = 'New password is required'
            elif not valid_password(old_password, account):
                message = 'Incorrect old password; please try again'
            elif new_password != confirm_new_password:
                message = 'Passwords do not match'
            else:
                check_csrf(csrf_token)
                account.hashed = bcrypt.hashpw(new_password, bcrypt.gensalt())
                raise HTTPRedirect('homepage?message={}', 'Your password has been updated')

        return {'message': message}

    # print out a CSV list of attendees that signed up for the newsletter for import into our bulk mailer
    @csv_file
    def can_spam(self, out, session):
        out.writerow(["fullname", "email", "zipcode"])
        for a in session.query(Attendee).filter_by(can_spam=True).order_by('email').all():
            out.writerow([a.full_name, a.email, a.zip_code])

    # print out a CSV list of staffers (ignore can_spam for this since it's for internal staff mailing)
    @csv_file
    def staff_emails(self, out, session):
        out.writerow(["fullname", "email", "zipcode"])
        for a in session.query(Attendee).filter_by(staffing=True, placeholder=False).order_by('email').all():
            out.writerow([a.full_name, a.email, a.zip_code])

    @unrestricted
    def insert_test_admin(self, session):
        if session.insert_test_admin_account():
            msg = "Test admin account created successfully"
        else:
            msg = "Not allowed to create admin account at this time"

        raise HTTPRedirect('login?message={}', msg)

    @unrestricted
    def sitemap(self):
        site_sections = cherrypy.tree.apps[c.CHERRYPY_MOUNT_PATH].root
        modules = {name: getattr(site_sections, name) for name in dir(site_sections) if not name.startswith('_')}
        pages = defaultdict(list)
        access_set = AdminAccount.access_set()
        for module_name, module_root in modules.items():
            for name in dir(module_root):
                method = getattr(module_root, name)
                if getattr(method, 'exposed', False):
                    spec = inspect.getfullargspec(unwrap(method))
                    has_defaults = len([arg for arg in spec.args[1:] if arg != 'session']) == len(spec.defaults or [])
                    if set(getattr(method, 'restricted', []) or []).intersection(access_set) \
                            and not getattr(method, 'ajax', False) \
                            and (getattr(method, 'site_mappable', False)
                                 or has_defaults and not spec.varkw):

                        pages[module_name].append({
                            'name': name.replace('_', ' ').title(),
                            'path': '/{}/{}'.format(module_name, name)
                        })

        return {'pages': sorted(pages.items())}

    @ajax
    def add_bulk_admin_accounts(self, session, message='', **params):
        ids = params.get('ids')
        if isinstance(ids, str):
            ids = str(ids).split(",")
        success_count = 0
        for id in ids:
            try:
                uuid.UUID(id)
            except ValueError:
                pass
            else:
                match = session.query(Attendee).filter(Attendee.id == id).first()
                if match:
                    account = session.admin_account(params, checkgroups=['access'])
                    if account.is_new:
                        password = genpasswd()
                        account.hashed = bcrypt.hashpw(password, bcrypt.gensalt())
                        account.attendee = match
                        session.add(account)
                        body = render('emails/accounts/new_account.txt', {
                            'account': account,
                            'password': password
                        }, encoding=None)
                        send_email.delay(
                            c.ADMIN_EMAIL,
                            match.email,
                            'New ' + c.EVENT_NAME + ' Ubersystem Account',
                            body,
                            model=match.to_dict('id'))

                        success_count += 1
        if success_count == 0:
            message = 'No new accounts were created.'
        else:
            session.commit()
            message = '%d new accounts have been created, and emailed their passwords.' % success_count
        return message
