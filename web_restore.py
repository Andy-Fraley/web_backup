#!/usr/bin/env python

import sys
import logging
import re
import boto3
import datetime
import os
import argparse
import urllib
import tempfile
import subprocess
from util import util


# Fake class only for purpose of limiting global namespace to the 'g' object
class g:
    program_filename = None 


def main(argv):

    parser = argparse.ArgumentParser()
    parser.add_argument('--from-website-backup-file', required=False, help='Input ZIP filename of a website '
        'backup file.')
    parser.add_argument('--from-s3-website-latest', required=False, help='Name of website with backup archive in S3.')
    parser.add_argument('--to-website-name', required=True, help='Name of local website to restore into.')
    parser.add_argument('--message-output-filename', required=False, help='Filename of message output file. If ' \
        'unspecified, then messages are written to stderr as well as into the messages_[datetime_stamp].log file ' \
        'that is zipped into the resulting backup file.')
    parser.add_argument('--zip-file-password', required=False, help='If provided, overrides password used to encryt ' \
        'zip file that is created that was specified in web_backup.ini')
    parser.add_argument('--aws-s3-bucket-name', required=False, help='AWS S3 bucket where output backup zip files ' \
        'are stored')

    g.args = parser.parse_args()

    g.program_filename = os.path.basename(__file__)
    if g.program_filename[-3:] == '.py':
        g.program_filename = g.program_filename[:-3]

    message_level = util.get_ini_setting('logging', 'level')

    if g.args.from_website_backup_file is None and g.args.from_s3_website_latest is None:
        message_error('Must either specify a local backup ZIP file to restore from or an S3 bucket to grab latest ' \
            'backup from.')
        util.sys_exit(1)

    if g.args.from_website_backup_file is None:

        # Load AWS creds which are used for iterating S3 backups and creating download link
        aws_access_key_id = util.get_ini_setting('aws', 'access_key_id', False)
        aws_secret_access_key = util.get_ini_setting('aws', 'secret_access_key', False)
        aws_region_name = util.get_ini_setting('aws', 'region_name', False)
        aws_s3_bucket_name = util.get_ini_setting('aws', 's3_bucket_name', False)

        # Find latest backup in 'daily' folder of S3 bucket 'ingomarchurch_website_backups'
        s3 = boto3.resource('s3', aws_access_key_id=aws_access_key_id, aws_secret_access_key=aws_secret_access_key,
            region_name=aws_region_name)
        file_items = [item for item in s3.Bucket(aws_s3_bucket_name).objects.all() if item.key[-1] != '/']
        newest_sortable_str = ''
        obj_to_retrieve = None
        for file_item in file_items:
            path_sects = file_item.key.split('/')
            if len(path_sects) == 3:
                if path_sects[0] == g.args.from_s3_website_latest:
                    if path_sects[1] == 'daily':
                        filename = path_sects[2]
                        match = re.match('(?P<year>[0-9]{4})(?P<month>[0-9]{2})(?P<day>[0-9]{2})' + \
                            '(?P<hours>[0-9]{2})(?P<minutes>[0-9]{2})(?P<seconds>[0-9]{2})\.zip', filename)
                        if match is not None:
                            sortable_str = match.group('year') + match.group('month') + match.group('day') + \
                                match.group('hours') + match.group('minutes') + match.group('seconds')
                            if sortable_str > newest_sortable_str:
                                newest_sortable_str = sortable_str
                                obj_to_retrieve = file_item
                        else:
                            message("Unrecognized file in 'daily' backup folder...ignoring: " + file_item.key)
                    else:
                        message('Non-matching folder or file in website_backups S3 bucket...ignoring: ' +
                            file_item.key)
            else:
                message('Unrecognized folder or file in website_backups S3 bucket with long path...ignoring: ' +
                    file_item.key)
        if obj_to_retrieve is not None:
            # Generate 10-minute download URL
            s3Client = boto3.client('s3', aws_access_key_id=aws_access_key_id,
                aws_secret_access_key=aws_secret_access_key, region_name=aws_region_name)
            url = s3Client.generate_presigned_url('get_object', Params = {'Bucket': aws_s3_bucket_name,
                'Key': obj_to_retrieve.key}, ExpiresIn = 10 * 60)
        else:
            message('Error finding latest backup file to retrieve. Aborting!')
            sys.exit(1)

        backup_zip_filename = os.path.abspath(os.path.dirname(__file__)) + '/tmp/' + g.args.from_s3_website_latest + \
            '_' +  newest_sortable_str + '.zip'
        urllib.urlretrieve(url, backup_zip_filename)
    else:
        if os.path.exists(g.args.from_website_backup_file):
            backup_zip_filename = g.args.from_website_backup_file
        else:
            message_error('Specified website backup file does not exist: ' + g.args.from_website_backup_file)
            sys.exit(1)

    if g.args.zip_file_password is not None:
        zip_file_password = g.args.zip_file_password
    else:
        zip_file_password = util.get_ini_setting('zip_file', 'password', False)

    temp_directory = tempfile.mkdtemp(prefix='web_restore_')
    exec_zip_list = ['/usr/bin/unzip', '-P', zip_file_password, backup_zip_filename, '-d', temp_directory]
    message_info('Unzipping backup file container into ' + temp_directory)
    FNULL = open(os.devnull, 'w')
    exit_status = subprocess.call(exec_zip_list, stdout=FNULL)

    sys.exit(0)


def message(str):
    global g

    datetime_stamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    #print >> sys.stderr, datetime_stamp + ':' + g.program_filename + ':' + level + ':' + s
    print >> sys.stderr, datetime_stamp + ':' + g.program_filename + ':' + ':' + str


def message_info(s):
    logging.info(s)
    output_message(s, 'INFO')


def message_warning(s):
    logging.warning(s)
    output_message(s, 'WARNING')


def message_error(s):
    logging.error(s)
    output_message(s, 'ERROR')


def output_message(s, level):
    global g

    if g.args.message_output_filename is None:
        datetime_stamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        print >> sys.stderr, datetime_stamp + ':' + g.program_filename + ':' + level + ':' + s


if __name__ == "__main__":
    main(sys.argv[1:])
