import json
import os
from datetime import timedelta

import arrow
import flask_profiler
import sentry_sdk
from coinbase_commerce.error import WebhookInvalidPayload, SignatureVerificationError
from coinbase_commerce.webhook import Webhook
from flask import (
    Flask,
    redirect,
    url_for,
    render_template,
    request,
    jsonify,
    flash,
    session,
    g,
)
from flask_admin import Admin
from flask_cors import cross_origin, CORS
from flask_login import current_user
from sentry_sdk.integrations.flask import FlaskIntegration
from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
from werkzeug.middleware.proxy_fix import ProxyFix

from app import paddle_utils, s3, config
from app.admin_model import (
    SLAdminIndexView,
    UserAdmin,
    EmailLogAdmin,
    AliasAdmin,
    MailboxAdmin,
    LifetimeCouponAdmin,
    ManualSubscriptionAdmin,
    ClientAdmin,
)
from app.api.base import api_bp
from app.auth.base import auth_bp
from app.config import (
    DB_URI,
    FLASK_SECRET,
    SENTRY_DSN,
    URL,
    SHA1,
    PADDLE_MONTHLY_PRODUCT_ID,
    RESET_DB,
    FLASK_PROFILER_PATH,
    FLASK_PROFILER_PASSWORD,
    SENTRY_FRONT_END_DSN,
    FIRST_ALIAS_DOMAIN,
    SESSION_COOKIE_NAME,
    PLAUSIBLE_HOST,
    PLAUSIBLE_DOMAIN,
    GITHUB_CLIENT_ID,
    GOOGLE_CLIENT_ID,
    FACEBOOK_CLIENT_ID,
    LANDING_PAGE_URL,
    STATUS_PAGE_URL,
    SUPPORT_EMAIL,
    get_abs_path,
    PADDLE_MONTHLY_PRODUCT_IDS,
    PADDLE_YEARLY_PRODUCT_IDS,
    PGP_SIGNER,
    COINBASE_WEBHOOK_SECRET,
    ROOT_DIR,
)
from app.dashboard.base import dashboard_bp
from app.developer.base import developer_bp
from app.discover.base import discover_bp
from app.email_utils import send_email, render
from app.extensions import db, login_manager, migrate, limiter
from app.jose_utils import get_jwk_key
from app.log import LOG
from app.models import (
    Client,
    User,
    ClientUser,
    Alias,
    RedirectUri,
    Subscription,
    PlanEnum,
    ApiKey,
    CustomDomain,
    LifetimeCoupon,
    Directory,
    Mailbox,
    Referral,
    AliasMailbox,
    Notification,
    CoinbaseSubscription,
    EmailLog,
    File,
    Contact,
    RefusedEmail,
    ManualSubscription,
)
from app.monitor.base import monitor_bp
from app.oauth.base import oauth_bp
from app.pgp_utils import load_public_key

if SENTRY_DSN:
    LOG.d("enable sentry")
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        release=f"app@{SHA1}",
        integrations=[
            FlaskIntegration(),
            SqlalchemyIntegration(),
        ],
    )

# the app is served behind nginx which uses http and not https
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"


def create_light_app() -> Flask:
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = DB_URI
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    db.init_app(app)

    return app


def create_app() -> Flask:
    app = Flask(__name__)
    # SimpleLogin is deployed behind NGINX
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_host=1)
    limiter.init_app(app)

    app.url_map.strict_slashes = False

    app.config["SQLALCHEMY_DATABASE_URI"] = DB_URI
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    # enable to print all queries generated by sqlalchemy
    # app.config["SQLALCHEMY_ECHO"] = True

    app.secret_key = FLASK_SECRET

    app.config["TEMPLATES_AUTO_RELOAD"] = True

    # to have a "fluid" layout for admin
    app.config["FLASK_ADMIN_FLUID_LAYOUT"] = True

    # to avoid conflict with other cookie
    app.config["SESSION_COOKIE_NAME"] = SESSION_COOKIE_NAME
    if URL.startswith("https"):
        app.config["SESSION_COOKIE_SECURE"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    setup_error_page(app)

    init_extensions(app)
    register_blueprints(app)
    set_index_page(app)
    jinja2_filter(app)

    setup_favicon_route(app)
    setup_openid_metadata(app)

    init_admin(app)
    setup_paddle_callback(app)
    setup_coinbase_commerce(app)
    setup_do_not_track(app)

    if FLASK_PROFILER_PATH:
        LOG.d("Enable flask-profiler")
        app.config["flask_profiler"] = {
            "enabled": True,
            "storage": {"engine": "sqlite", "FILE": FLASK_PROFILER_PATH},
            "basicAuth": {
                "enabled": True,
                "username": "admin",
                "password": FLASK_PROFILER_PASSWORD,
            },
            "ignore": ["^/static/.*", "/git", "/exception"],
        }
        flask_profiler.init_app(app)

    # enable CORS on /api endpoints
    CORS(app, resources={r"/api/*": {"origins": "*"}})

    # set session to permanent so user stays signed in after quitting the browser
    # the cookie is valid for 7 days
    @app.before_request
    def make_session_permanent():
        session.permanent = True
        app.permanent_session_lifetime = timedelta(days=7)

    return app


def fake_data():
    LOG.d("create fake data")
    # Remove db if exist
    if os.path.exists("db.sqlite"):
        LOG.d("remove existing db file")
        os.remove("db.sqlite")

    # Create all tables
    db.create_all()

    # Create a user
    user = User.create(
        email="john@wick.com",
        name="John Wick",
        password="password",
        activated=True,
        is_admin=True,
        # enable_otp=True,
        otp_secret="base32secret3232",
        intro_shown=True,
        fido_uuid=None,
    )
    user.trial_end = None
    db.session.commit()

    # add a profile picture
    file_path = "profile_pic.svg"
    s3.upload_from_bytesio(
        file_path,
        open(os.path.join(ROOT_DIR, "static", "default-icon.svg"), "rb"),
        content_type="image/svg",
    )
    file = File.create(user_id=user.id, path=file_path, commit=True)
    user.profile_picture_id = file.id
    db.session.commit()

    # create a bounced email
    alias = Alias.create_new_random(user)
    db.session.commit()

    bounce_email_file_path = "bounce.eml"
    s3.upload_email_from_bytesio(
        bounce_email_file_path,
        open(os.path.join(ROOT_DIR, "local_data", "email_tests", "2.eml"), "rb"),
        "download.eml",
    )
    refused_email = RefusedEmail.create(
        path=bounce_email_file_path,
        full_report_path=bounce_email_file_path,
        user_id=user.id,
        commit=True,
    )

    contact = Contact.create(
        user_id=user.id,
        alias_id=alias.id,
        website_email="hey@google.com",
        reply_email="rep@sl.local",
        commit=True,
    )
    EmailLog.create(
        user_id=user.id,
        contact_id=contact.id,
        refused_email_id=refused_email.id,
        bounced=True,
        commit=True,
    )

    LifetimeCoupon.create(code="coupon", nb_used=10, commit=True)

    # Create a subscription for user
    Subscription.create(
        user_id=user.id,
        cancel_url="https://checkout.paddle.com/subscription/cancel?user=1234",
        update_url="https://checkout.paddle.com/subscription/update?user=1234",
        subscription_id="123",
        event_time=arrow.now(),
        next_bill_date=arrow.now().shift(days=10).date(),
        plan=PlanEnum.monthly,
        commit=True,
    )

    CoinbaseSubscription.create(
        user_id=user.id, end_at=arrow.now().shift(days=10), commit=True
    )

    api_key = ApiKey.create(user_id=user.id, name="Chrome")
    api_key.code = "code"

    api_key = ApiKey.create(user_id=user.id, name="Firefox")
    api_key.code = "codeFF"

    pgp_public_key = open(get_abs_path("local_data/public-pgp.asc")).read()
    m1 = Mailbox.create(
        user_id=user.id,
        email="pgp@example.org",
        verified=True,
        pgp_public_key=pgp_public_key,
    )
    m1.pgp_finger_print = load_public_key(pgp_public_key)
    db.session.commit()

    for i in range(3):
        if i % 2 == 0:
            a = Alias.create(
                email=f"e{i}@{FIRST_ALIAS_DOMAIN}", user_id=user.id, mailbox_id=m1.id
            )
        else:
            a = Alias.create(
                email=f"e{i}@{FIRST_ALIAS_DOMAIN}",
                user_id=user.id,
                mailbox_id=user.default_mailbox_id,
            )
        db.session.commit()

        if i % 5 == 0:
            if i % 2 == 0:
                AliasMailbox.create(alias_id=a.id, mailbox_id=user.default_mailbox_id)
            else:
                AliasMailbox.create(alias_id=a.id, mailbox_id=m1.id)
        db.session.commit()

        # some aliases don't have any activity
        # if i % 3 != 0:
        #     contact = Contact.create(
        #         user_id=user.id,
        #         alias_id=a.id,
        #         website_email=f"contact{i}@example.com",
        #         reply_email=f"rep{i}@sl.local",
        #     )
        #     db.session.commit()
        #     for _ in range(3):
        #         EmailLog.create(user_id=user.id, contact_id=contact.id)
        #         db.session.commit()

        # have some disabled alias
        if i % 5 == 0:
            a.enabled = False
            db.session.commit()

    custom_domain1 = CustomDomain.create(user_id=user.id, domain="ab.cd", verified=True)
    db.session.commit()

    Alias.create(
        user_id=user.id,
        email="first@ab.cd",
        mailbox_id=user.default_mailbox_id,
        custom_domain_id=custom_domain1.id,
        commit=True,
    )

    Alias.create(
        user_id=user.id,
        email="second@ab.cd",
        mailbox_id=user.default_mailbox_id,
        custom_domain_id=custom_domain1.id,
        commit=True,
    )

    Directory.create(user_id=user.id, name="abcd")
    Directory.create(user_id=user.id, name="xyzt")
    db.session.commit()

    # Create a client
    client1 = Client.create_new(name="Demo", user_id=user.id)
    client1.oauth_client_id = "client-id"
    client1.oauth_client_secret = "client-secret"
    db.session.commit()

    RedirectUri.create(
        client_id=client1.id, uri="https://your-website.com/oauth-callback"
    )

    client2 = Client.create_new(name="Demo 2", user_id=user.id)
    client2.oauth_client_id = "client-id2"
    client2.oauth_client_secret = "client-secret2"
    db.session.commit()

    ClientUser.create(user_id=user.id, client_id=client1.id, name="Fake Name")

    referral = Referral.create(user_id=user.id, code="REFCODE", name="First referral")
    db.session.commit()

    for i in range(6):
        Notification.create(user_id=user.id, message=f"""Hey hey <b>{i}</b> """ * 10)
    db.session.commit()

    user2 = User.create(
        email="winston@continental.com",
        password="password",
        activated=True,
        referral_id=referral.id,
    )
    Mailbox.create(user_id=user2.id, email="winston2@high.table", verified=True)
    db.session.commit()

    ManualSubscription.create(
        user_id=user2.id,
        end_at=arrow.now().shift(years=1, days=1),
        comment="Local manual",
        commit=True,
    )


@login_manager.user_loader
def load_user(user_id):
    user = User.get(user_id)
    if user and user.disabled:
        return None

    return user


def register_blueprints(app: Flask):
    app.register_blueprint(auth_bp)
    app.register_blueprint(monitor_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(developer_bp)

    app.register_blueprint(oauth_bp, url_prefix="/oauth")
    app.register_blueprint(oauth_bp, url_prefix="/oauth2")

    app.register_blueprint(discover_bp)
    app.register_blueprint(api_bp)


def set_index_page(app):
    @app.route("/", methods=["GET", "POST"])
    def index():
        if current_user.is_authenticated:
            return redirect(url_for("dashboard.index"))
        else:
            return redirect(url_for("auth.login"))

    @app.after_request
    def after_request(res):
        # not logging /static call
        if (
            not request.path.startswith("/static")
            and not request.path.startswith("/admin/static")
            and not request.path.startswith("/_debug_toolbar")
        ):
            LOG.debug(
                "%s %s %s %s %s",
                request.remote_addr,
                request.method,
                request.path,
                request.args,
                res.status_code,
            )

        return res


def setup_openid_metadata(app):
    @app.route("/.well-known/openid-configuration")
    @cross_origin()
    def openid_config():
        res = {
            "issuer": URL,
            "authorization_endpoint": URL + "/oauth2/authorize",
            "token_endpoint": URL + "/oauth2/token",
            "userinfo_endpoint": URL + "/oauth2/userinfo",
            "jwks_uri": URL + "/jwks",
            "response_types_supported": [
                "code",
                "token",
                "id_token",
                "id_token token",
                "id_token code",
            ],
            "subject_types_supported": ["public"],
            "id_token_signing_alg_values_supported": ["RS256"],
            # todo: add introspection and revocation endpoints
            # "introspection_endpoint": URL + "/oauth2/token/introspection",
            # "revocation_endpoint": URL + "/oauth2/token/revocation",
        }

        return jsonify(res)

    @app.route("/jwks")
    @cross_origin()
    def jwks():
        res = {"keys": [get_jwk_key()]}
        return jsonify(res)


def get_current_user():
    try:
        return g.user
    except AttributeError:
        return current_user


def setup_error_page(app):
    @app.errorhandler(400)
    def bad_request(e):
        if request.path.startswith("/api/"):
            return jsonify(error="Bad Request"), 400
        else:
            return render_template("error/400.html"), 400

    @app.errorhandler(401)
    def unauthorized(e):
        if request.path.startswith("/api/"):
            return jsonify(error="Unauthorized"), 401
        else:
            flash("You need to login to see this page", "error")
            return redirect(url_for("auth.login", next=request.full_path))

    @app.errorhandler(403)
    def forbidden(e):
        if request.path.startswith("/api/"):
            return jsonify(error="Forbidden"), 403
        else:
            return render_template("error/403.html"), 403

    @app.errorhandler(429)
    def rate_limited(e):
        LOG.warning(
            "Client hit rate limit on path %s, user:%s",
            request.path,
            get_current_user(),
        )
        if request.path.startswith("/api/"):
            return jsonify(error="Rate limit exceeded"), 429
        else:
            return render_template("error/429.html"), 429

    @app.errorhandler(404)
    def page_not_found(e):
        if request.path.startswith("/api/"):
            return jsonify(error="No such endpoint"), 404
        else:
            return render_template("error/404.html"), 404

    @app.errorhandler(405)
    def wrong_method(e):
        if request.path.startswith("/api/"):
            return jsonify(error="Method not allowed"), 405
        else:
            return render_template("error/405.html"), 405

    @app.errorhandler(Exception)
    def error_handler(e):
        LOG.exception(e)
        if request.path.startswith("/api/"):
            return jsonify(error="Internal error"), 500
        else:
            return render_template("error/500.html"), 500


def setup_favicon_route(app):
    @app.route("/favicon.ico")
    def favicon():
        return redirect("/static/favicon.ico")


def jinja2_filter(app):
    def format_datetime(value):
        dt = arrow.get(value)
        return dt.humanize()

    app.jinja_env.filters["dt"] = format_datetime

    @app.context_processor
    def inject_stage_and_region():
        return dict(
            YEAR=arrow.now().year,
            URL=URL,
            SENTRY_DSN=SENTRY_FRONT_END_DSN,
            VERSION=SHA1,
            FIRST_ALIAS_DOMAIN=FIRST_ALIAS_DOMAIN,
            PLAUSIBLE_HOST=PLAUSIBLE_HOST,
            PLAUSIBLE_DOMAIN=PLAUSIBLE_DOMAIN,
            GITHUB_CLIENT_ID=GITHUB_CLIENT_ID,
            GOOGLE_CLIENT_ID=GOOGLE_CLIENT_ID,
            FACEBOOK_CLIENT_ID=FACEBOOK_CLIENT_ID,
            LANDING_PAGE_URL=LANDING_PAGE_URL,
            STATUS_PAGE_URL=STATUS_PAGE_URL,
            SUPPORT_EMAIL=SUPPORT_EMAIL,
            PGP_SIGNER=PGP_SIGNER,
        )


def setup_paddle_callback(app: Flask):
    @app.route("/paddle", methods=["GET", "POST"])
    def paddle():
        LOG.debug(f"paddle callback {request.form.get('alert_name')} {request.form}")

        # make sure the request comes from Paddle
        if not paddle_utils.verify_incoming_request(dict(request.form)):
            LOG.exception(
                "request not coming from paddle. Request data:%s", dict(request.form)
            )
            return "KO", 400

        if (
            request.form.get("alert_name") == "subscription_created"
        ):  # new user subscribes
            # the passthrough is json encoded, e.g.
            # request.form.get("passthrough") = '{"user_id": 88 }'
            passthrough = json.loads(request.form.get("passthrough"))
            user_id = passthrough.get("user_id")
            user = User.get(user_id)

            subscription_plan_id = int(request.form.get("subscription_plan_id"))

            if subscription_plan_id in PADDLE_MONTHLY_PRODUCT_IDS:
                plan = PlanEnum.monthly
            elif subscription_plan_id in PADDLE_YEARLY_PRODUCT_IDS:
                plan = PlanEnum.yearly
            else:
                LOG.exception(
                    "Unknown subscription_plan_id %s %s",
                    subscription_plan_id,
                    request.form,
                )
                return "No such subscription", 400

            sub = Subscription.get_by(user_id=user.id)

            if not sub:
                LOG.d(f"create a new Subscription for user {user}")
                Subscription.create(
                    user_id=user.id,
                    cancel_url=request.form.get("cancel_url"),
                    update_url=request.form.get("update_url"),
                    subscription_id=request.form.get("subscription_id"),
                    event_time=arrow.now(),
                    next_bill_date=arrow.get(
                        request.form.get("next_bill_date"), "YYYY-MM-DD"
                    ).date(),
                    plan=plan,
                )
            else:
                LOG.d(f"Update an existing Subscription for user {user}")
                sub.cancel_url = request.form.get("cancel_url")
                sub.update_url = request.form.get("update_url")
                sub.subscription_id = request.form.get("subscription_id")
                sub.event_time = arrow.now()
                sub.next_bill_date = arrow.get(
                    request.form.get("next_bill_date"), "YYYY-MM-DD"
                ).date()
                sub.plan = plan

                # make sure to set the new plan as not-cancelled
                # in case user cancels a plan and subscribes a new plan
                sub.cancelled = False

            LOG.debug("User %s upgrades!", user)

            db.session.commit()

        elif request.form.get("alert_name") == "subscription_payment_succeeded":
            subscription_id = request.form.get("subscription_id")
            LOG.debug("Update subscription %s", subscription_id)

            sub: Subscription = Subscription.get_by(subscription_id=subscription_id)
            # when user subscribes, the "subscription_payment_succeeded" can arrive BEFORE "subscription_created"
            # at that time, subscription object does not exist yet
            if sub:
                sub.event_time = arrow.now()
                sub.next_bill_date = arrow.get(
                    request.form.get("next_bill_date"), "YYYY-MM-DD"
                ).date()

                db.session.commit()

        elif request.form.get("alert_name") == "subscription_cancelled":
            subscription_id = request.form.get("subscription_id")

            sub: Subscription = Subscription.get_by(subscription_id=subscription_id)
            if sub:
                # cancellation_effective_date should be the same as next_bill_date
                LOG.warning(
                    "Cancel subscription %s %s on %s, next bill date %s",
                    subscription_id,
                    sub.user,
                    request.form.get("cancellation_effective_date"),
                    sub.next_bill_date,
                )
                sub.event_time = arrow.now()

                sub.cancelled = True
                db.session.commit()

                user = sub.user

                send_email(
                    user.email,
                    "SimpleLogin - what can we do to improve the product?",
                    render(
                        "transactional/subscription-cancel.txt",
                        end_date=request.form.get("cancellation_effective_date"),
                    ),
                )

            else:
                return "No such subscription", 400
        elif request.form.get("alert_name") == "subscription_updated":
            subscription_id = request.form.get("subscription_id")

            sub: Subscription = Subscription.get_by(subscription_id=subscription_id)
            if sub:
                LOG.debug(
                    "Update subscription %s %s on %s, next bill date %s",
                    subscription_id,
                    sub.user,
                    request.form.get("cancellation_effective_date"),
                    sub.next_bill_date,
                )
                if (
                    int(request.form.get("subscription_plan_id"))
                    == PADDLE_MONTHLY_PRODUCT_ID
                ):
                    plan = PlanEnum.monthly
                else:
                    plan = PlanEnum.yearly

                sub.cancel_url = request.form.get("cancel_url")
                sub.update_url = request.form.get("update_url")
                sub.event_time = arrow.now()
                sub.next_bill_date = arrow.get(
                    request.form.get("next_bill_date"), "YYYY-MM-DD"
                ).date()
                sub.plan = plan

                # make sure to set the new plan as not-cancelled
                sub.cancelled = False

                db.session.commit()
            else:
                return "No such subscription", 400
        return "OK"


def setup_coinbase_commerce(app):
    @app.route("/coinbase", methods=["POST"])
    def coinbase_webhook():
        # event payload
        request_data = request.data.decode("utf-8")
        # webhook signature
        request_sig = request.headers.get("X-CC-Webhook-Signature", None)

        try:
            # signature verification and event object construction
            event = Webhook.construct_event(
                request_data, request_sig, COINBASE_WEBHOOK_SECRET
            )
        except (WebhookInvalidPayload, SignatureVerificationError) as e:
            LOG.exception("Invalid Coinbase webhook")
            return str(e), 400

        LOG.d("Coinbase event %s", event)

        if event["type"] == "charge:confirmed":
            if handle_coinbase_event(event):
                return "success", 200
            else:
                return "error", 400

        return "success", 200


def handle_coinbase_event(event) -> bool:
    user_id = int(event["data"]["metadata"]["user_id"])
    code = event["data"]["code"]
    user = User.get(user_id)
    if not user:
        LOG.exception("User not found %s", user_id)
        return False

    coinbase_subscription: CoinbaseSubscription = CoinbaseSubscription.get_by(
        user_id=user_id
    )

    if not coinbase_subscription:
        LOG.d("Create a coinbase subscription for %s", user)
        coinbase_subscription = CoinbaseSubscription.create(
            user_id=user_id, end_at=arrow.now().shift(years=1), code=code, commit=True
        )
        send_email(
            user.email,
            "Your SimpleLogin account has been upgraded",
            render(
                "transactional/coinbase/new-subscription.txt",
                coinbase_subscription=coinbase_subscription,
            ),
            render(
                "transactional/coinbase/new-subscription.html",
                coinbase_subscription=coinbase_subscription,
            ),
        )
    else:
        if coinbase_subscription.code != code:
            LOG.d("Update code from %s to %s", coinbase_subscription.code, code)
            coinbase_subscription.code = code

        if coinbase_subscription.is_active():
            coinbase_subscription.end_at = coinbase_subscription.end_at.shift(years=1)
        else:  # already expired subscription
            coinbase_subscription.end_at = arrow.now().shift(years=1)

        db.session.commit()

        send_email(
            user.email,
            "Your SimpleLogin account has been extended",
            render(
                "transactional/coinbase/extend-subscription.txt",
                coinbase_subscription=coinbase_subscription,
            ),
            render(
                "transactional/coinbase/extend-subscription.html",
                coinbase_subscription=coinbase_subscription,
            ),
        )

    return True


def init_extensions(app: Flask):
    login_manager.init_app(app)
    db.init_app(app)
    migrate.init_app(app)


def init_admin(app):
    admin = Admin(name="SimpleLogin", template_mode="bootstrap4")

    admin.init_app(app, index_view=SLAdminIndexView())
    admin.add_view(UserAdmin(User, db.session))
    admin.add_view(AliasAdmin(Alias, db.session))
    admin.add_view(MailboxAdmin(Mailbox, db.session))
    admin.add_view(EmailLogAdmin(EmailLog, db.session))
    admin.add_view(LifetimeCouponAdmin(LifetimeCoupon, db.session))
    admin.add_view(ManualSubscriptionAdmin(ManualSubscription, db.session))
    admin.add_view(ClientAdmin(Client, db.session))


def setup_do_not_track(app):
    @app.route("/dnt")
    def do_not_track():
        return """
        <script src="/static/local-storage-polyfill.js"></script>

        <script>
// Disable Analytics if this script is called

store.set('analytics-ignore', 't');

alert("Analytics disabled");

window.location.href = "/";

</script>
        """


def local_main():
    config.COLOR_LOG = True
    app = create_app()

    # enable flask toolbar
    # from flask_debugtoolbar import DebugToolbarExtension
    # app.config["DEBUG_TB_PROFILER_ENABLED"] = True
    # app.config["DEBUG_TB_INTERCEPT_REDIRECTS"] = False
    # app.debug = True
    # DebugToolbarExtension(app)

    # warning: only used in local
    if RESET_DB:
        from init_app import add_sl_domains

        LOG.warning("reset db, add fake data")
        with app.app_context():
            fake_data()
            add_sl_domains()

    app.run(debug=True, port=7778)

    # uncomment to run https locally
    # LOG.d("enable https")
    # import ssl
    # context = ssl.SSLContext(ssl.PROTOCOL_TLSv1_2)
    # context.load_cert_chain("local_data/cert.pem", "local_data/key.pem")
    # app.run(debug=True, port=7777, ssl_context=context)


if __name__ == "__main__":
    local_main()
