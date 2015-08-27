import hashlib
import time
from bson.objectid import ObjectId
from gridfs import GridFS
from flask import current_app as app
from eve.utils import ParsedRequest
from requests import request
from PIL import Image
from io import BytesIO
from kombu.serialization import register
import dill
from celery import chord, group

from pytineye.api import TinEyeAPIRequest, TinEyeAPIError

import superdesk
from superdesk.celery_app import celery

from .logging import error, warning, info, success
from .elastic import handle_elastic_write_problems_wrapper
from .exceptions import APIGracefulException, ImageNotFoundException
from .vpp_mock import (
    activate_izitru_mock, activate_tineye_mock, activate_incandescent_mock
)
from .incandescent import (
    get_incandescent_results, get_incandescent_results_callback
)


# @TODO: for debug purpose
from pprint import pprint  # noqa
from .logging import debug  # noqa


register('dill', dill.dumps, dill.loads, content_type='application/x-binary-data', content_encoding='binary')


def init_tineye(app):
    app.data.vpp_tineye_api = TinEyeAPIRequest(
        api_url=app.config['TINEYE_API_URL'],
        public_key=app.config['TINEYE_PUBLIC_KEY'],
        private_key=app.config['TINEYE_SECRET_KEY']
    )


def get_original_image(item):
    if 'renditions' in item:
        driver = app.data.mongo
        px = driver.current_mongo_prefix('ingest')
        _fs = GridFS(driver.pymongo(prefix=px).db)
        for k, v in item['renditions'].items():
            if k == 'original':
                _file = _fs.get(ObjectId(v['media']))
                content = _file.read()
                href = v['href']
                return (href, content)
    raise ImageNotFoundException()


def get_tineye_results(content):
    try:
        response = superdesk.app.data.vpp_tineye_api.search_data(content)
    except TinEyeAPIError as e:
        # @TODO: or e.message[0] == 'NO_SIGNATURE_ERROR' ?
        if e.code == 400:
            return {'total': None, 'results': {"status": "error", "message": repr(e.message)}}
        raise APIGracefulException(e)
    except KeyError as e:
        if e.args[0] == 'code':
            raise APIGracefulException(e)
    result = response.json_results
    if 'results' not in result or 'total_results' not in result['results']:
        raise APIGracefulException(result)
    return {'total': result['results']['total_results'], 'results': result}


def get_izitru_results(filename, content):
    izitru_security_data = int(time.time())
    m = hashlib.md5()
    m.update(str(izitru_security_data).encode())
    m.update(superdesk.app.config['IZITRU_PRIVATE_KEY'].encode())
    izitru_security_hash = m.hexdigest()

    upfile = content
    img = Image.open(BytesIO(content))
    if img.format != 'JPEG':
        exif = img.info.get('exif', b"")
        converted_image = BytesIO()
        img.save(converted_image, 'JPEG', exif=exif)
        upfile = converted_image.getvalue()
        converted_image.close()
    img.close()

    data = {
        'activationKey': superdesk.app.config['IZITRU_ACTIVATION_KEY'],
        'securityData': izitru_security_data,
        'securityHash': izitru_security_hash,
        'exactMatch': 'true',
        'nearMatch': 'false',
        'storeImage': 'true',
    }
    files = {'upFile': (filename, upfile, 'image/jpeg', {'Expires': '0'})}
    response = request('POST', superdesk.app.config['IZITRU_API_URL'], data=data, files=files)
    if response.status_code != 200:
        raise APIGracefulException(response)
    result = response.json()
    if 'verdict' not in result:
        raise APIGracefulException(result)
    return {'total': result['verdict'], 'results': result}


API_GETTERS = {
    'izitru': {"function": get_izitru_results, "args": ("filename", "content",)},
    'tineye': {"function": get_tineye_results, "args": ("content",)},
    # 'incandescent': {"function": get_incandescent_results, "args": ("href",)},
}


MOCKS = {
    'izitru': {
        'function': activate_izitru_mock,
        'fixtures': [
            {"response_file": "test/vpp/mock_izitru_1.json"},
            {"response_file": "test/vpp/mock_izitru_3.json"},
            {"response_file": "test/vpp/mock_izitru_5.json"},
            {"response": {'status': 'error', 'message': 'something gone wrong'}}
        ]
    },
    'tineye': {
        'function': activate_tineye_mock,
        'fixtures': [
            {"response_file": "test/vpp/mock_tineye_many.json"},
            {"response_file": "test/vpp/mock_tineye_zero.json"},
            {"response": {'status': 'error', 'message': 'something gone wrong'}}
        ]
    },
    'incandescent': {
        'function': activate_incandescent_mock,
        'fixtures': [
            {"response_file": './test/vpp/incandescent_add_response.json'},
        ]
    },
    'incandescent_callback': {
        'function': activate_incandescent_mock,
        'fixtures': [
            {"response_file": './test/vpp/incandescent_result_response.json'}
        ]
    },
}


def get_api_getter(api_name, api_getter=None):
    if not api_getter:
        api_getter = API_GETTERS[api_name]['function']
    if app.config['USE_VERIFICATION_MOCK']:
        mock = MOCKS[api_name]
        api_wrapper = mock['function'](*mock['fixtures'])
        api_getter = api_wrapper(api_getter)
    return api_getter


@celery.task(max_retries=3, bind=True, serializer='dill', name='vpp.append_api_result', ignore_result=False)
def append_api_results_to_item(self, item, api_name, args, verification_id):
    filename = item['slugline']
    info(
        "{api}: searching matches for {file}... ({tries} of {max})".format(
            api=api_name, file=filename, tries=self.request.retries, max=self.max_retries
        ))
    try:
        results_object = get_api_getter(api_name)(*args)
    except APIGracefulException as e:
        if self.request.retries < self.max_retries:
            warning("{api}: API exception raised during "
                    "verification of {file} (retrying):\n {exception}".format(
                        api=api_name, file=filename, exception=e))
            raise self.retry(
                exc=e, countdown=app.config['VERIFICATION_TASK_RETRY_INTERVAL'])
        else:
            error("{api}: max retries exceeded on "
                  "verification of {file}:\n {exception}".format(
                      api=api_name, file=filename, exception=e
                  ))
            verification_result = {"status": "error", "message": repr(e)}
            results_number = None
    else:
        info("{api}: matchs found for {file}.".format(
            api=api_name, file=filename
        ))
        verification_result = results_object['results']
        results_number = results_object['total']
    # record result to database
    handle_elastic_write_problems_wrapper(
        lambda: superdesk.get_resource_service('ingest').patch(
            item['_id'],
            {'verification.{api}'.format(api=api_name): results_number}
        )
    )
    handle_elastic_write_problems_wrapper(
        lambda: superdesk.get_resource_service('verification_results').patch(
            verification_id,
            {'{api}'.format(api=api_name): verification_result}
        )
    )


@celery.task(max_retries=3, bind=True, serializer='dill', name='vpp.append_incandescent_result', ignore_result=False)
def append_incandescent_results_to_item(self, item, href):
    api_name = 'incandescent'
    filename = item['slugline']
    info(
        "{api}: searching matches for {file}... ({tries} of {max})".format(
            api=api_name, file=filename, tries=self.request.retries, max=self.max_retries
        ))
    try:
        get_data = get_api_getter(api_name, get_incandescent_results)(href)
    except APIGracefulException as e:
        if self.request.retries < self.max_retries:
            warning("{api}: API exception raised during "
                    "verification of {file} (retrying):\n {exception}".format(
                        api=api_name, file=filename, exception=e))
            raise self.retry(exc=e, countdown=app.config['VERIFICATION_TASK_RETRY_INTERVAL'])
        else:
            error("{api}: max retries exceeded on "
                  "verification of {file}:\n {exception}".format(
                      api=api_name, file=filename, exception=e))
            verification_result = {"status": "error", "message": repr(e)}
            return verification_result
    else:
        info("{api}: waiting for matches for {file}.".format(
            api=api_name, file=filename))
        return get_data


@celery.task(
    max_retries=20, countdown=5, bind=True,
    name='vpp.append_incandescent_result_callback', ignore_result=False
)
def append_incandescent_results_to_item_callback(self, get_data, item_id, filename, verification_id):
    api_name = 'incandescent'

    if 'status' in get_data:
        verification_result = get_data
    else:
        try:
            raw_results = get_api_getter(
                'incandescent_callback', get_incandescent_results_callback
            )(get_data)
        except APIGracefulException as e:
            if self.request.retries < self.max_retries:
                raise self.retry(exc=e)
            else:
                error("{api}: timeout exceeded on "
                      "verification of {file}:\n {exception}".format(
                          api=api_name, file=filename, exception=e))
                verification_result = {"status": "error", "message": repr(e)}
        else:
            info("{api}: matchs found for {file}.".format(
                api=api_name, file=filename))

        verification_result = {}
        for url, data in raw_results.items():
            for page_n, page in data['pages'].items():
                source = 'incandescent_' + page['source']
                key = url.replace('.', '_')
                if source not in verification_result:
                    verification_result[source] = {}
                if key not in verification_result[source]:
                    verification_result[source][key] = {}
                verification_result[source][key][page_n] = page

    # record result to database
    handle_elastic_write_problems_wrapper(
        lambda: superdesk.get_resource_service('ingest').patch(
            item_id,
            {"verification.{source}".format(source=source): len(results)
                for source, results in verification_result.items()}
        )
    )
    handle_elastic_write_problems_wrapper(
        lambda: superdesk.get_resource_service('verification_results').patch(
            verification_id,
            verification_result
        )
    )
    return


@celery.task(bind=True, name='vpp.finalize_verification', ignore_result=False)
def finalize_verification(self, *args, item_id, desk_id):
    success('Fetching item: {} into desk "Verified Images".'.format(item_id))
    handle_elastic_write_problems_wrapper(
        # Auto fetch items to the 'Verified Images' desk
        lambda: superdesk.get_resource_service('fetch').fetch(
            [{'_id': item_id, 'desk': desk_id}]
        )
    )
    handle_elastic_write_problems_wrapper(
        # Delete the ingest item
        lambda: superdesk.get_resource_service('ingest').delete(
            lookup={'_id': item_id}
        )
    )


@celery.task(ignore_result=False, name='vpp.wait_for_results')
def wait_for_results(*args, **kwargs):
    debug((args, kwargs, ))
    return


@celery.task(bind=True, name='vpp.verify_ingest')
def verify_ingest(self):
    info(
        'Checking for new ingested images for verification...'
    )
    try:
        items = superdesk.get_resource_service('ingest').get_from_mongo(
            req=ParsedRequest(),
            lookup={
                'type': 'picture',
                'verification': {'$exists': False}
            }
        )
        desk = superdesk.get_resource_service('desks').find_one(req=ParsedRequest(), name='Verified Images')
        desk_id = str(desk['_id'])
    except Exception as e:
        error("Raised from verify_ingest task, aborting:")
        error(e)
        raise(e)
    for item in items:
        verification_id = handle_elastic_write_problems_wrapper(
            lambda: superdesk.get_resource_service('verification_results').post([{}])
        )[0]
        handle_elastic_write_problems_wrapper(
            lambda: superdesk.get_resource_service('ingest').patch(
                item['_id'],
                {'verification.results': verification_id}
            )
        )
        filename = item['slugline']
        info(
            'found new ingested item: "{}"'.format(filename)
        )
        try:
            href, content = get_original_image(item)
        except ImageNotFoundException:
            return
        all_args = {
            "filename": filename,
            "content": content,
            "href": href
        }
        verification_tasks = group(
            (
                chord([
                    append_api_results_to_item.subtask(
                        args=(
                            item, api_name,
                            [all_args[arg_name] for arg_name in data['args']]
                        ),
                        kwargs={
                            'verification_id': verification_id
                        }
                    )
                    for api_name, data in API_GETTERS.items()
                ],
                    wait_for_results.subtask()
                )
            ),
            (
                append_incandescent_results_to_item.subtask(
                    kwargs={
                        "item": item,
                        "href": all_args['href'],
                    }
                ) | append_incandescent_results_to_item_callback.subtask(
                    kwargs={
                        "filename": all_args['filename'],
                        "item_id": item['_id'],
                        'verification_id': verification_id
                    }
                )
            )
        )
        chord(
            verification_tasks,
            finalize_verification.subtask(
                kwargs={"item_id": item['_id'], "desk_id": desk_id},
                immutable=True
            )
        ).delay()
