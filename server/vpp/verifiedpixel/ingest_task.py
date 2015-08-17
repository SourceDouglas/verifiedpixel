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

from apiclient.discovery import build as google_build
from apiclient.discovery import HttpError as GoogleHttpError

import superdesk
from superdesk.celery_app import celery

from .logging import error, warning, info, success
from .elastic import (
    handle_elastic_timeout_decorator, handle_elastic_write_problems_wrapper
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


class ImageNotFoundException(Exception):
    pass


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


class APIGracefulException(Exception):
    pass


def get_tineye_results(content):
    try:
        response = superdesk.app.data.vpp_tineye_api.search_data(content)
    except TinEyeAPIError as e:
        raise APIGracefulException(e)
    except KeyError as e:
        if e.args[0] == 'code':
            raise APIGracefulException(e)
    else:
        return response.json_results


def get_gris_results(href):
    try:
        service = google_build('customsearch', 'v1',
                               developerKey=superdesk.app.config['GRIS_API_KEY'])
        res = service.cse().list(
            q=href,
            searchType='image',
            cx=superdesk.app.config['GRIS_API_CX'],
        ).execute()
    except GoogleHttpError as e:
        raise APIGracefulException(e)
    return res


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
    return result


API_GETTERS = {
    'izitru': {"function": get_izitru_results, "args": ("filename", "content",)},
    'tineye': {"function": get_tineye_results, "args": ("content",)},
    'gris': {"function": get_gris_results, "args": ("href",)},
}


# @TODO: replace it to a settings' option to use mocks
USE_MOCKS = False
if USE_MOCKS:
    def get_placeholder_results(*args, **kwargs):
        return {"status": "ok", "message": "this is mock"}
    API_GETTERS = {
        'izitru': {"function": get_placeholder_results, "args": ("filename", "content",)},
        'tineye': {"function": get_placeholder_results, "args": ("content",)},
        'gris': {"function": get_placeholder_results, "args": ("href",)},
    }


@celery.task(max_retries=3, bind=True, serializer='dill', name='vpp.append_api_result', ignore_result=False)
@handle_elastic_timeout_decorator()
def append_api_results_to_item(self, item, api_name, args):
    filename = item['slugline']
    api_getter = API_GETTERS[api_name]['function']
    info(
        "{api}: searching matches for {file}... ({tries} of {max})".format(
            api=api_name, file=filename, tries=self.request.retries, max=self.max_retries
        ))
    try:
        verification_result = api_getter(*args)
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
    else:
        info("{api}: matchs found for {file}.".format(
            api=api_name, file=filename))
    # record result to database
    handle_elastic_write_problems_wrapper(
        lambda: superdesk.get_resource_service('ingest').patch(
            item['_id'],
            {'verification.{api}'.format(api=api_name): verification_result}
        )
    )


@celery.task(bind=True, name='vpp.finalize_verification')
@handle_elastic_timeout_decorator()
def finalize_verification(self, *args, item_id, desk_id):
    # Auto fetch items to the 'Verified Imges' desk
    success('Fetching item: {} into desk "Verified Images".'.format(item_id))
    superdesk.get_resource_service('fetch').fetch([{'_id': item_id, 'desk': desk_id}])
    # Delete the ingest item
    superdesk.get_resource_service('ingest').delete(lookup={'_id': item_id})


@celery.task(bind=True, name='vpp.verify_ingest')
@handle_elastic_timeout_decorator()
def verify_ingest(self):
    info(
        'Checking for new ingested images for verification...'
    )
    items = superdesk.get_resource_service('ingest').get_from_mongo(
        req=ParsedRequest(),
        lookup={
            'type': 'picture',
            'verification': {'$exists': False}
        }
    )
    desk = superdesk.get_resource_service('desks').find_one(req=ParsedRequest(), name='Verified Images')
    desk_id = str(desk['_id'])

    for item in items:
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
        chord(
            group(
                (append_api_results_to_item.subtask(
                    args=(
                        item, api_name,
                        [all_args[arg_name] for arg_name in data['args']]
                    )
                ))
                for api_name, data in API_GETTERS.items()
            ),
            finalize_verification.subtask(
                kwargs={"item_id": item['_id'], "desk_id": desk_id},
                immutable=True
            )
        ).delay()
