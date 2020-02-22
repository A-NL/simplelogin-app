import re

import arrow
from flask import render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user
from flask_wtf import FlaskForm
from wtforms import StringField, validators, ValidationError

from app.config import EMAIL_DOMAIN
from app.config import PAGE_LIMIT
from app.dashboard.base import dashboard_bp
from app.email_utils import get_email_part
from app.extensions import db
from app.log import LOG
from app.models import ForwardEmailLog
from app.models import GenEmail, ForwardEmail
from app.utils import random_string


class AliasLog:
    website_email: str
    website_from: str
    alias: str
    when: arrow.Arrow
    is_reply: bool
    blocked: bool

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


def get_alias_log(gen_email: GenEmail, page_id=0):
    logs: [AliasLog] = []

    q = (
        db.session.query(ForwardEmail, ForwardEmailLog)
        .filter(ForwardEmail.id == ForwardEmailLog.forward_id)
        .filter(ForwardEmail.gen_email_id == gen_email.id)
        .order_by(ForwardEmailLog.id.desc())
        .limit(PAGE_LIMIT)
        .offset(page_id * PAGE_LIMIT)
    )

    for fe, fel in q:
        al = AliasLog(
            website_email=fe.website_email,
            website_from=fe.website_from,
            alias=gen_email.email,
            when=fel.created_at,
            is_reply=fel.is_reply,
            blocked=fel.blocked,
        )
        logs.append(al)
    logs = sorted(logs, key=lambda l: l.when, reverse=True)

    return logs


@dashboard_bp.route("/alias/<int:alias_id>/activity/<int:page_id>")
@dashboard_bp.route("/alias/<int:alias_id>/activity", defaults={"page_id": 0})
@login_required
def alias_log(alias_id, page_id):
    alias = gen_email = GenEmail.get(alias_id)

    # sanity check
    if not gen_email:
        flash("You do not have access to this page", "warning")
        return redirect(url_for("dashboard.index"))

    if gen_email.user_id != current_user.id:
        flash("You do not have access to this page", "warning")
        return redirect(url_for("dashboard.index"))

    logs = get_alias_log(gen_email, page_id)
    base = (
        db.session.query(ForwardEmail, ForwardEmailLog)
        .filter(ForwardEmail.id == ForwardEmailLog.forward_id)
        .filter(ForwardEmail.gen_email_id == gen_email.id)
    )
    total = base.count()
    email_forwarded = (
        base.filter(ForwardEmailLog.is_reply == False)
        .filter(ForwardEmailLog.blocked == False)
        .count()
    )
    email_replied = base.filter(ForwardEmailLog.is_reply == True).count()
    email_blocked = base.filter(ForwardEmailLog.blocked == True).count()
    last_page = (
        len(logs) < PAGE_LIMIT
    )  # lightweight pagination without counting all objects

    return render_template("dashboard/alias_detail/log.html", **locals())


def email_validator():
    """validate email address. Handle both only email and email with name:
    - ab@cd.com
    - AB CD <ab@cd.com>

    """
    message = "Invalid email format. Email must be either email@example.com or *First Last <email@example.com>*"

    def _check(form, field):
        email = field.data
        email = email.strip()
        email_part = email

        if "<" in email and ">" in email:
            if email.find("<") + 1 < email.find(">"):
                email_part = email[email.find("<") + 1 : email.find(">")].strip()

        if re.match(r"^[A-Za-z0-9\.\+_-]+@[A-Za-z0-9\._-]+\.[a-zA-Z]*$", email_part):
            return

        raise ValidationError(message)

    return _check


class NewContactForm(FlaskForm):
    email = StringField(
        "Email", validators=[validators.DataRequired(), email_validator()]
    )


@dashboard_bp.route("/alias/<int:alias_id>/contact_manager/", methods=["GET", "POST"])
@dashboard_bp.route(
    "/alias/<int:alias_id>/contact_manager/<int:forward_email_id>",
    methods=["GET", "POST"],
)
@login_required
def alias_contact_manager(alias_id, forward_email_id=None):
    alias = gen_email = GenEmail.get(alias_id)

    # sanity check
    if not gen_email:
        flash("You do not have access to this page", "warning")
        return redirect(url_for("dashboard.index"))

    if gen_email.user_id != current_user.id:
        flash("You do not have access to this page", "warning")
        return redirect(url_for("dashboard.index"))

    new_contact_form = NewContactForm()

    if request.method == "POST":
        if request.form.get("form-name") == "create":
            if new_contact_form.validate():
                contact_email = new_contact_form.email.data.strip()

                # generate a reply_email, make sure it is unique
                # not use while to avoid infinite loop
                for _ in range(1000):
                    reply_email = f"ra+{random_string(25)}@{EMAIL_DOMAIN}"
                    if not ForwardEmail.get_by(reply_email=reply_email):
                        break

                website_email = get_email_part(contact_email)

                # already been added
                if ForwardEmail.get_by(
                    gen_email_id=gen_email.id, website_email=website_email
                ):
                    flash(f"{website_email} is already added", "error")
                    return redirect(
                        url_for("dashboard.alias_contact_manager", alias_id=alias_id)
                    )

                forward_email = ForwardEmail.create(
                    gen_email_id=gen_email.id,
                    website_email=website_email,
                    website_from=contact_email,
                    reply_email=reply_email,
                )

                LOG.d("create reverse-alias for %s", contact_email)
                db.session.commit()
                flash(f"Reverse alias for {contact_email} is created", "success")

                return redirect(
                    url_for(
                        "dashboard.alias_contact_manager",
                        alias_id=alias_id,
                        forward_email_id=forward_email.id,
                        alias=alias,
                    )
                )
        elif request.form.get("form-name") == "delete":
            forward_email_id = request.form.get("forward-email-id")
            forward_email = ForwardEmail.get(forward_email_id)

            if not forward_email:
                flash("Unknown error. Refresh the page", "warning")
                return redirect(
                    url_for("dashboard.alias_contact_manager", alias_id=alias_id)
                )
            elif forward_email.gen_email_id != gen_email.id:
                flash("You cannot delete reverse-alias", "warning")
                return redirect(
                    url_for("dashboard.alias_contact_manager", alias_id=alias_id)
                )

            contact_name = forward_email.website_from
            ForwardEmail.delete(forward_email_id)
            db.session.commit()

            flash(f"Reverse-alias for {contact_name} has been deleted", "success")

            return redirect(
                url_for("dashboard.alias_contact_manager", alias_id=alias_id)
            )

    # make sure highlighted forward_email is at array start
    forward_emails = gen_email.forward_emails

    if forward_email_id:
        forward_emails = sorted(
            forward_emails, key=lambda fe: fe.id == forward_email_id, reverse=True
        )

    return render_template(
        "dashboard/alias_detail/contact_manager.html",
        forward_emails=forward_emails,
        alias=alias,
        new_contact_form=new_contact_form,
        forward_email_id=forward_email_id,
    )
