"""
Initializer for the API application. This will create a new Flask app and
register all interface versions (Blueprints), initialize the database and
register app level error handlers.
"""
import re
import json
import atexit

from flask import Flask, current_app, Response
from flask.ext.cors import CORS
from server.web.utils import compose_error


def create_app():
    """
    Create the api as it's own app so that it's easier to scale it out on it's
    own in the future.

    :return:         A flask object/wsgi callable.
    """
    import cf_deployment_tracker
    from bluemix_service_discovery.service_publisher import ServicePublisher
    from server.config import Config
    from os import environ as env
    from server.exceptions import APIException
    from server.web.utils import request_wants_json
    from server.web.rest.demos import demos_v1_blueprint, setup_auth_from_request
    from server.web.rest.shipments import shipments_v1_blueprint
    from server.web.rest.distribution_centers import distribution_centers_v1_blueprint
    from server.web.rest.retailers import retailers_v1_blueprint
    from server.web.rest.products import products_v1_blueprint

    # Emit Bluemix deployment event
    cf_deployment_tracker.track()

    # Create the app
    logistics_wizard = Flask('logistics_wizard', static_folder='ui_dist')
    # logistics_wizard.debug = True

    @logistics_wizard.route('/')
    def root():
        return logistics_wizard.send_static_file('index.html')

    @logistics_wizard.route('/<path:path>')
    def static_proxy(path):
      # send_static_file will guess the correct MIME type
      return logistics_wizard.send_static_file(path)

    CORS(logistics_wizard, origins=[re.compile('.*')], supports_credentials=True)
    if Config.ENVIRONMENT == 'DEV':
        logistics_wizard.debug = True

    # Register the blueprints for each component
    logistics_wizard.register_blueprint(demos_v1_blueprint, url_prefix='/api/v1')
    logistics_wizard.register_blueprint(shipments_v1_blueprint, url_prefix='/api/v1')
    logistics_wizard.register_blueprint(distribution_centers_v1_blueprint, url_prefix='/api/v1')
    logistics_wizard.register_blueprint(retailers_v1_blueprint, url_prefix='/api/v1')
    logistics_wizard.register_blueprint(products_v1_blueprint, url_prefix='/api/v1')

    logistics_wizard.before_request(setup_auth_from_request)

    def exception_handler(e):
        """
        Handle any exception thrown in the interface layer and return
        a JSON response with the error details. Wraps python exceptions
        with a generic exception message.

        :param e:  The raised exception.
        :return:   A Flask response object.
        """
        if not isinstance(e, APIException):
            exc = APIException(u'Server Error',
                               internal_details=unicode(e))
        else:
            exc = e
        current_app.logger.error(exc)
        return Response(json.dumps(compose_error(exc, e)),
                        status=exc.status_code,
                        mimetype='application/json')

    def not_found_handler(e):
        current_app.logger.exception(e)
        if request_wants_json():
            status_code = 404
            return Response(json.dumps({
                                'code': status_code,
                                'message': 'Resource not found.'
                            }),
                            status=status_code,
                            mimetype='application/json')
        else:
            # TODO: Default to the root web page
            # return index()
            pass

    def bad_request_handler(e):
        current_app.logger.exception(e)
        status_code = 400
        return Response(json.dumps({
                            'code': status_code,
                            'message': 'Bad request.'
                        }),
                        status=status_code,
                        mimetype='application/json')

    # Register error handlers
    logistics_wizard.errorhandler(Exception)(exception_handler)
    logistics_wizard.errorhandler(400)(bad_request_handler)
    logistics_wizard.errorhandler(404)(not_found_handler)

    # Register app with Service Discovery and initiate heartbeat cycle if running in PROD
    if Config.SD_STATUS == 'ON' and env.get('VCAP_APPLICATION') is not None:
        from signal import signal, SIGINT, SIGTERM
        from sys import exit

        # Create service publisher and register service
        creds = json.loads(env['VCAP_SERVICES'])['service_discovery'][0]['credentials']
        publisher = ServicePublisher('lw-controller', 300, 'UP',
                                     json.loads(env['VCAP_APPLICATION'])['application_uris'][0],
                                     'http', tags=['logistics-wizard', 'front-end', env['LOGISTICS_WIZARD_ENV']],
                                     url=creds['url'], auth_token=creds['auth_token'])
        publisher.register_service(True)

        # Set up exit handlers for gracefully killing heartbeat thread
        def exit_app(*args):
            deregister_app(publisher)
            exit(0)
        signal(SIGTERM, exit_app)
        signal(SIGINT, exit_app)
        atexit.register(destroy_app, publisher)

    return logistics_wizard


def deregister_app(publisher):
    """
    Deregister the app and stop its heartbeat (if beating)

    :param: publisher   Service Discovery publisher
    """
    if publisher is not None and publisher.registered:
        print ("Deregistering service from Service Discovery")
        publisher.deregister_service()


def destroy_app(publisher):
    """
    Gracefully shuts down the controller app

    :param: publisher   Service Discovery publisher
    """
    deregister_app(publisher)
