import ast
import csv
import getpass
import glob
import logging
import os
import sys
import time
import requests
import ast
import ee
import requests
import retrying
from requests_toolbelt.multipart import encoder
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver import Firefox
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By

if sys.version_info > (3, 0):
    from urllib.parse import unquote
else:
    from urllib import unquote

from google.cloud import storage

from metadata_loader import load_metadata_from_csv, validate_metadata_from_csv
pathway=os.path.dirname(os.path.realpath(__file__))
ee.Initialize()
os.chdir(os.path.dirname(os.path.realpath(__file__)))
lp=os.path.dirname(os.path.realpath(__file__))
def tabup(user, source_path, destination_path, metadata_path=None, multipart_upload=False, nodata_value=None, bucket_name=None):
    submitted_tasks_id = {}

    __verify_path_for_upload(destination_path)

    path = os.path.join(os.path.expanduser(source_path), '*.zip')
    all_images_paths = glob.glob(path)
    if len(all_images_paths) == 0:
        logging.error('%s does not contain any tif images.', path)
        sys.exit(1)

    if user is not None:
        password = getpass.getpass()
        google_session = __get_google_auth_session(user, password)
    else:
        storage_client = storage.Client()

    __create_image_collection(destination_path)

    images_for_upload_path = __find_remaining_assets_for_upload(all_images_paths, destination_path)
    no_images = len(images_for_upload_path)

    if no_images == 0:
        logging.error('No images found that match %s. Exiting...', path)
        sys.exit(1)

    failed_asset_writer = FailedAssetsWriter()

    for current_image_no, image_path in enumerate(images_for_upload_path):
        #print('Processing image '+str(current_image_no+1)+' of '+str(no_images)+' '+str(os.path.basename(image_path)))
        filename = __get_filename_from_path(path=image_path)

        asset_full_path = destination_path + '/' + filename

        try:
            if user is not None:
                gsid = __upload_file_gee(s=google_session,
                                                  file_path=image_path,
                                                  use_multipart=multipart_upload)
            else:
                gsid = __upload_file_gcs(storage_client, bucket_name, image_path)
            output=subprocess.check_output('earthengine upload table --asset_id '+str(asset_full_path)+' '+str(gsid),shell=True)
            print('Ingesting '+str(current_image_no+1)+' of '+str(no_images)+' '+str(os.path.basename(asset_full_path))+' '+str(output).strip())
        except Exception as e:
            logging.exception('Upload of %s has failed.', filename)

def __create_asset_request(asset_full_path, gsid):
    return {"id": asset_full_path,
        "tilesets": [
            {"sources": [
                {"primaryPath": gsid,
                 "additionalPaths": []
                 }
            ]}
        ],
    }

def __verify_path_for_upload(path):
    folder = path[:path.rfind('/')]
    response = ee.data.getInfo(folder)
    if not response:
        logging.error('%s is not a valid destination. Make sure full path is provided e.g. users/user/nameofcollection '
                      'or projects/myproject/myfolder/newcollection and that you have write access there.', path)
        sys.exit(1)


def __find_remaining_assets_for_upload(path_to_local_assets, path_remote):
    local_assets = [__get_filename_from_path(path) for path in path_to_local_assets]
    if __collection_exist(path_remote):
        remote_assets = __get_asset_names_from_collection(path_remote)
        if len(remote_assets) > 0:
            assets_left_for_upload = set(local_assets) - set(remote_assets)
            if len(assets_left_for_upload) == 0:
                logging.warning('Collection already exists and contains all assets provided for upload. Exiting ...')
                sys.exit(1)

            logging.info('Collection already exists. %d assets left for upload to %s.', len(assets_left_for_upload), path_remote)
            assets_left_for_upload_full_path = [path for path in path_to_local_assets
                                                if __get_filename_from_path(path) in assets_left_for_upload]
            return assets_left_for_upload_full_path

    return path_to_local_assets


def retry_if_ee_error(exception):
    return isinstance(exception, ee.EEException)


@retrying.retry(retry_on_exception=retry_if_ee_error, wait_exponential_multiplier=1000, wait_exponential_max=4000, stop_max_attempt_number=3)
def __start_ingestion_task(asset_request):
    task_id = ee.data.newTaskId(1)[0]
    _ = ee.data.startIngestion(task_id, asset_request)
    return task_id


def __get_google_auth_session(username, password):
    options = Options()
    options.add_argument('-headless')
    authorization_url="https://code.earthengine.google.com"
    uname=username
    passw=password
    driver = Firefox(executable_path=os.path.join(pathway,"geckodriver.exe"),firefox_options=options)
    driver.get(authorization_url)
    time.sleep(5)
    username = driver.find_element_by_xpath('//*[@id="identifierId"]')
    username.send_keys(uname)
    driver.find_element_by_id("identifierNext").click()
    time.sleep(5)
    passw=driver.find_element_by_name("password").send_keys(passw)
    driver.find_element_by_id("passwordNext").click()
    time.sleep(5)
    try:
        driver.find_element_by_xpath("//div[@id='view_container']/form/div[2]/div/div/div/ul/li/div/div[2]/p").click()
        time.sleep(5)
        driver.find_element_by_xpath("//div[@id='submit_approve_access']/content/span").click()
        time.sleep(5)
    except Exception as e:
        pass
    cookies = driver.get_cookies()
    s = requests.Session()
    print('Session authentication completed')
    r=s.get("https://code.earthengine.google.com/assets/upload/geturl")
    d = ast.literal_eval(r.text)
    print d['url']
    return d['url']
    return s

def __get_upload_url(s):
    # get url and discard; somehow it does not work for the first time
    r=s.get("https://code.earthengine.google.com/assets/upload/geturl")
    d = ast.literal_eval(r.text)
    print d['url']
    return d['url']

@retrying.retry(retry_on_exception=retry_if_ee_error, wait_exponential_multiplier=1000, wait_exponential_max=4000, stop_max_attempt_number=3)
def __upload_file_gee(s, file_path, use_multipart):

    with open(file_path, 'rb') as f:
        upload_url = __get_upload_url(s)


        if use_multipart:
            form = MultipartEncoder({
                "documents": (file_path, f, "application/octet-stream"),
                "composite": "NONE",
            })
            headers = {"Prefer": "respond-async", "Content-Type": form.content_type}
            resp = s.post(upload_url, headers=headers, data=form)
        else:
            files = {'file': f}
            resp = s.post(upload_url, files=files)

        gsid = resp.json()[0]
        #print('GSID',gsid)

        return gsid

@retrying.retry(retry_on_exception=retry_if_ee_error, wait_exponential_multiplier=1000, wait_exponential_max=4000, stop_max_attempt_number=3)
def __upload_file_gcs(storage_client, bucket_name, image_path):
    bucket = storage_client.get_bucket(bucket_name)
    blob_name = __get_filename_from_path(path=image_path)
    blob = bucket.blob(blob_name)

    blob.upload_from_filename(image_path)

    url = 'gs://' + bucket_name + '/' + blob_name

    return url

def __periodic_check(current_image, period, tasks, writer):
    if (current_image + 1) % period == 0:
        logging.info('Periodic check')
        __check_for_failed_tasks_and_report(tasks=tasks, writer=writer)
        # Time to check how many tasks are running!
        __wait_for_tasks_to_complete(waiting_time=10, no_allowed_tasks_running=20)


def __check_for_failed_tasks_and_report(tasks, writer):
    if len(tasks) == 0:
        return

    statuses = ee.data.getTaskStatus(tasks.keys())

    for status in statuses:
        if status['state'] == 'FAILED':
            task_id = status['id']
            filename = tasks[task_id]
            error_message = status['error_message']
            writer.writerow([filename, task_id, error_message])
            logging.error('Ingestion of image %s has failed with message %s', filename, error_message)

    tasks.clear()


def __get_filename_from_path(path):
    return os.path.splitext(os.path.basename(os.path.normpath(path)))[0]


def __get_number_of_running_tasks():
    return len([task for task in ee.data.getTaskList() if task['state'] == 'RUNNING'])


def __wait_for_tasks_to_complete(waiting_time, no_allowed_tasks_running):
    tasks_running = __get_number_of_running_tasks()
    while tasks_running > no_allowed_tasks_running:
        logging.info('Number of running tasks is %d. Sleeping for %d s until it goes down to %d',
                     tasks_running, waiting_time, no_allowed_tasks_running)
        time.sleep(waiting_time)
        tasks_running = __get_number_of_running_tasks()


def __collection_exist(path):
    return True if ee.data.getInfo(path) else False


def __create_image_collection(full_path_to_collection):
    if __collection_exist(full_path_to_collection):
        logging.warning("Collection %s already exists", full_path_to_collection)
    else:
        ee.data.createAsset({'type': ee.data.ASSET_TYPE_FOLDER}, full_path_to_collection)
        print('New folder '+str(full_path_to_collection)+' created')


def __get_asset_names_from_collection(collection_path):
    assets_list = ee.data.getList(params={'id': collection_path})
    assets_names = [os.path.basename(asset['id']) for asset in assets_list]
    return assets_names


class FailedAssetsWriter(object):

    def __init__(self):
        self.initialized = False

    def writerow(self, row):
        if not self.initialized:
            if sys.version_info > (3, 0):
                self.failed_upload_file = open('failed_upload.csv', 'w')
            else:
                self.failed_upload_file = open('failed_upload.csv', 'wb')
            self.failed_upload_writer = csv.writer(self.failed_upload_file)
            self.failed_upload_writer.writerow(['filename', 'task_id', 'error_msg'])
            self.initialized = True
        self.failed_upload_writer.writerow(row)

    def close(self):
        if self.initialized:
            self.failed_upload_file.close()
            self.initialized = False
