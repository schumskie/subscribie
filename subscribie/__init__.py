# -*- coding: utf-8 -*-
"""
    subscribie.app
    ~~~~~~~~~
    A microframework for buiding subsciption websites.
    This module implements the central subscribie application.

    :copyright: (c) 2018 by Karma Computing Ltd
"""
import logging
from dotenv import load_dotenv
import os
import sys
import sqlite3
from .database import database
import flask
import datetime
from base64 import b64encode
import git
from flask import (
    Flask,
    render_template,
    session,
    url_for,
    current_app,
    Blueprint,
)

import beeline
from beeline.middleware.flask import HoneyMiddleware

from .Template import load_theme
from flask_cors import CORS
from flask_uploads import configure_uploads, UploadSet, IMAGES, patch_request_class
import importlib
from importlib import reload
import urllib
from pathlib import Path
import sqlalchemy
from flask_migrate import Migrate
import click
from jinja2 import Template
from flask_mail import Mail, Message

from .models import (
    PaymentProvider,
    Person,
    Company,
    Module,
)

from .blueprints.admin import get_subscription_status

load_dotenv(verbose=True)
PYTHON_LOG_LEVEL = os.environ.get("PYTHON_LOG_LEVEL", "WARNING")
logging.basicConfig(level=PYTHON_LOG_LEVEL)

beeline.init(
    writekey=os.environ.get("HONEYCOMB_API_KEY"),
    dataset="subscribie",
    service_name="subscribie",
)


def seed_db():
    pass


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    HoneyMiddleware(
        app, db_events=True
    )  # db_events defaults to True, set to False if not using our db middleware with Flask-SQLAlchemy
    load_dotenv(verbose=True)
    app.config.update(os.environ)

    if test_config is not None:
        app.config.update(test_config)

    @app.before_request
    def start_session():
        try:
            session["sid"]
        except KeyError:
            session["sid"] = urllib.parse.quote_plus(b64encode(os.urandom(10)))
            print("Starting with sid {}".format(session["sid"]))

    @app.before_first_request
    def register_modules():
        """Import any custom modules"""
        # Set custom modules path
        sys.path.append(app.config["MODULES_PATH"])
        modules = Module.query.all()
        print("sys.path contains: {}".format(sys.path))
        for module in modules:
            # Assume standard python module
            try:
                print("Attempting to importing module: {}".format(module.name))
                importlib.import_module(module.name)
            except ModuleNotFoundError:
                # Attempt to load module from src
                dest = Path(app.config["MODULES_PATH"], module.name)
                print("Cloning module into: {}".format(dest))
                os.makedirs(str(dest), exist_ok=True)
                try:
                    git.Repo.clone_from(module.src, dest)
                except git.exc.GitCommandError:
                    pass
                # Now re-try import
                try:
                    import site

                    reload(site)
                    importlib.import_module(module.name)
                except ModuleNotFoundError:
                    print("Error: Could not import module: {}".format(module.name))
            # Register modules as blueprint (if it is one)
            try:
                importedModule = importlib.import_module(module.name)
                if isinstance(getattr(importedModule, module.name), Blueprint):
                    # Load any config the Blueprint declares
                    blueprint = getattr(importedModule, module.name)
                    blueprintConfig = "".join([blueprint.root_path, "/", "config.py"])
                    app.config.from_pyfile(blueprintConfig, silent=True)
                    # Register the Blueprint
                    app.register_blueprint(getattr(importedModule, module.name))
                    print(f"Imported {module.name} as flask Blueprint")

            except (ModuleNotFoundError, AttributeError):
                print(
                    "Error: Could not import module as blueprint: {}".format(
                        module.name
                    )
                )

    CORS(app, resources={r"/api/*": {"origins": "*"}})
    CORS(app, resources={r"/auth/jwt-login/*": {"origins": "*"}})
    images = UploadSet("images", IMAGES)
    patch_request_class(app, int(app.config.get("MAX_CONTENT_LENGTH", 2 * 1024 * 1024)))
    configure_uploads(app, images)

    from . import auth
    from . import views
    from . import api

    app.register_blueprint(auth.bp)
    app.register_blueprint(views.bp)
    app.register_blueprint(api.api)
    from .blueprints.admin import admin
    from .blueprints.subscriber import subscriber
    from .blueprints.pages import module_pages
    from .blueprints.iframe import module_iframe_embed
    from .blueprints.style import module_style_shop
    from .blueprints.seo import module_seo_page_title

    app.register_blueprint(module_pages, url_prefix="/pages")
    app.register_blueprint(module_iframe_embed, url_prefix="/iframe")
    app.register_blueprint(module_style_shop, url_prefix="/style")
    app.register_blueprint(module_seo_page_title, url_prefix="/seo")
    app.register_blueprint(admin, url_prefix="/admin")
    app.register_blueprint(subscriber)

    app.add_url_rule("/", "index", views.__getattribute__("choose"))

    with app.app_context():

        database.init_app(app)
        Migrate(app, database)

        try:
            payment_provider = PaymentProvider.query.first()
            if payment_provider is None:
                # If payment provider table not seeded, seed with blank values.
                payment_provider = PaymentProvider()
                database.session.add(payment_provider)
                database.session.commit()
        except sqlalchemy.exc.OperationalError as e:
            # Allow to fail until migrations run (flask upgrade requires app reboot)
            print(e)

        load_theme(app)

    # Handling Errors Gracefully
    @app.errorhandler(404)
    def page_not_found(e):
        return render_template("errors/404.html"), 404

    @app.errorhandler(413)
    def request_entity_too_large(error):
        return "File Too Large", 413

    @app.errorhandler(500)
    def page_error_500(e):
        return render_template("errors/500.html"), 500

    @app.cli.command()
    def initdb():
        """Initialize the database."""
        click.echo("Init the db")
        with open("seed.sql") as fp:
            con = sqlite3.connect(app.config["DB_FULL_PATH"])
            cur = con.cursor()
            # Check not already seeded
            cur.execute("SELECT id from user")
            if cur.fetchone() is None:
                cur.executescript(fp.read())
            else:
                print("Database already seeded.")
            con.close()

    @app.cli.command()
    def alert_subscribers_make_choice():
        """Alert qualifying subscribers to set their choices

        For all people (aka Subscribers)

        - Loop over their *active* subscriptions
        - Check if x days before their subscription.next_date
        - If yes, sent them an email alert
        """

        def alert_subscriber_update_choices(subscriber: Person):
            email_template = str(
                Path(current_app.root_path + "/emails/update-choices.jinja2.html")
            )
            # App context needed for request.host (app.config["SERVER_NAME"] not set)
            with app.test_request_context("/"):
                update_options_url = (
                    "https://" + flask.request.host + url_for("subscriber.login")
                )
                company = Company.query.first()
                with open(email_template) as file_:
                    template = Template(file_.read())
                    html = template.render(
                        update_options_url=update_options_url, company=company
                    )
                    try:
                        mail = Mail(current_app)
                        msg = Message()
                        msg.subject = company.name + " " + "Update Options"
                        msg.sender = current_app.config["EMAIL_LOGIN_FROM"]
                        msg.recipients = [person.email]
                        msg.html = html
                        mail.send(msg)
                    except Exception as e:
                        print(e)
                        print("Failed to send update choices email")

        people = Person.query.all()

        for person in people:
            for subscription in person.subscriptions:
                if (
                    get_subscription_status(subscription.gocardless_subscription_id)
                    == "active"
                ):
                    # Check if x days until next subscription due, make configurable
                    today = datetime.date.today()
                    days_until = subscription.next_date().date() - today
                    if days_until.days == 8:
                        print(
                            f"Sending alert for subscriber '{person.id}' on \
                              plan: {subscription.plan.title}"
                        )
                        alert_subscriber_update_choices(person)

    return app
