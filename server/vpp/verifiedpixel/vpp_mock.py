from httmock import urlmatch, HTTMock
import json
import re
from unittest import mock

from pprint import pprint  # noqa

from urllib3.connectionpool import HTTPConnectionPool
orig_urlopen = HTTPConnectionPool.urlopen
from urllib3_mock import Responses  # noqa

from .logging import debug  # noqa


METADATA = 0
CONTENT = 1


def prepare_sequence_from_args(args):
    sequence = []
    for arg in args:
        status = arg.get('status', 200)
        response = arg.get('response', None)
        if isinstance(response, dict):
            response = json.dumps(response)
        if not response:
            fixture_path = arg['response_file']
            with open(fixture_path, 'r') as f:
                response = f.read()
        sequence.append(({'status': status}, response))
    return sequence


def create_eternal_fixture_generator(fixtures_list):
    i = 0
    fixtures = [x for x in prepare_sequence_from_args(fixtures_list)]
    max = len(fixtures)
    while True:
        yield fixtures[i]
        i = (i + 1) % max


def get_fixture_generator(fixtures, eternal=False):
    if eternal:
        return create_eternal_fixture_generator(fixtures)
    else:
        return (fixture for fixture in prepare_sequence_from_args(fixtures))


def activate_izitru_mock(*fixtures, eternal=False):
    fixture_generator = get_fixture_generator(fixtures, eternal)

    @urlmatch(
        scheme='https', netloc='www.izitru.com', path='/scripts/uploadAPI.pl'
    )
    def izitru_request(url, request):
        debug("served requests mock for IZITRU")
        fixture = next(fixture_generator)
        return {
            'status_code': fixture[METADATA]['status'],
            'content': json.loads(fixture[CONTENT]),
        }

    def wrap(f):
        def test_new(*args):
            with HTTMock(izitru_request):
                return f(*args)
        return test_new
    return wrap


def activate_incandescent_mock(*fixtures, eternal=False):
    fixture_generator = get_fixture_generator(fixtures, eternal)

    @urlmatch(
        scheme='https', netloc='incandescent.xyz'
    )
    def incandescent_request(url, request):
        debug("served requests mock for INCANDESCENT")
        fixture = next(fixture_generator)
        return {
            'status_code': fixture[METADATA]['status'],
            'content': json.loads(fixture[CONTENT]),
        }

    def wrap(f):
        def test_new(*args):
            with HTTMock(incandescent_request):
                return f(*args)
        return test_new
    return wrap


def activate_tineye_mock(*fixtures, eternal=False):
    responses = Responses('urllib3')
    fixture_generator = get_fixture_generator(fixtures, eternal)

    def tineye_response(request):
        debug("served urllib3 mock for TINEYE")
        fixture = next(fixture_generator)
        return (fixture[METADATA]['status'], {}, fixture[CONTENT])
    responses.add_callback(
        'POST', re.compile(r'.*api\.tineye\.com/rest/search/.*'),
        callback=tineye_response
    )

    def pass_through(req):
        '''
        workaround for elasticsearch which uses urllib3 as well
        '''
        '''
        logger.debug("urllib3-PASS-THROUGH: " + str(
            (req.method, req.host, req.port, req.url, )
        ))
        '''
        new_params = {}
        for key in ['method', 'headers', 'body', 'url']:
            new_params[key] = getattr(req, key)
        http = HTTPConnectionPool(host=req.host, port=req.port)
        res = orig_urlopen(http, **new_params)
        return (res.status, res.getheaders(), res.data)
    responses.add_callback(mock.ANY, re.compile(r'/.*'), callback=pass_through)

    def wrap(f):
        @responses.activate
        def test_new(*args):
            return f(*args)
        return test_new
    return wrap
