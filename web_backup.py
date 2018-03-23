#!/usr/bin/env python

import sys
import datetime
import logging
import argparse
import os
import shutil
import tempfile
import subprocess
import ConfigParser
import re
import calendar
import boto3
from util import util
import pytz
import glob

# Fake class only for purpose of limiting global namespace to the 'g' object
class g:
    args = None
    program_filename = None
    temp_directory = None
    message_output_filename = None
    aws_access_key_id = None
    aws_secret_access_key = None
    aws_region_name = None
    aws_s3_bucket_name = None
    reuse_output_filename = None
    run_util_errors = None
    website_directory = None
    websites = None


def main(argv):
    global g

    parser = argparse.ArgumentParser()
    parser.add_argument('--output-filename', required=False,
        help='Output ZIP filename. Defaults to ./tmp/<website_name>_[datetime_stamp].zip')
    parser.add_argument('--message-output-filename', required=False, help='Filename of message output file. If ' \
        'unspecified, then messages are written to stderr as well as into the messages_[datetime_stamp].log file ' \
        'that is zipped into the resulting backup file.')
    parser.add_argument('--post-to-s3', action='store_true', help='If specified, then the created zip file is ' \
        'posted to Amazon AWS S3 bucket (using bucket URL and password in web_backup.ini file)')
    parser.add_argument('--delete-zip', action='store_true', help='If specified, then the created zip file is ' \
        'deleted after posting to S3')
    parser.add_argument('--website-name', required=False, help='Specified website name is mapped to its ' \
        'hosting directory under /var/www and its contents are recursively zipped and if website is WordPress, ' \
        'wp-config.php is interogated and database .sql backup file created and included in ecrypted zip archive ' \
        'which is posted to S3.  If no --website-name is specified, then all websites on this server are listed')
    parser.add_argument('--retain-temp-directory', action='store_true', help='If specified, the temp directory ' +
        'with output from website directory and WordPress database is not deleted')
    parser.add_argument('--show-backups-to-do', action='store_true', help='If specified, the ONLY thing that is ' +
        'done is backup posts and deletions to S3 are calculated and displayed')
    parser.add_argument('--zip-file-password', required=False, help='If provided, overrides password used to encryt ' \
        'zip file that is created that was specified in web_backup.ini')
    parser.add_argument('--aws-s3-bucket-name', required=False, help='AWS S3 bucket where output backup zip files ' \
        'are stored')
    parser.add_argument('--notification-emails', required=False, nargs='*', default=argparse.SUPPRESS,
        help='If specified, list of email addresses that are emailed upon successful upload to AWS S3, along with ' \
        'accessor link to get at the backup zip file (which is encrypted)')

    g.args = parser.parse_args()

    g.program_filename = os.path.basename(__file__)
    if g.program_filename[-3:] == '.py':
                g.program_filename = g.program_filename[:-3]

    message_level = util.get_ini_setting('logging', 'level')

    util.set_logger(message_level, g.message_output_filename, os.path.basename(__file__))

    g.websites = util.get_websites()
    if g.args.website_name is None or g.args.website_name not in g.websites.keys():
        if g.args.website_name is None:
            print 'NOTE:  --website-name of website to backup was not specified.'
        else:
            print 'NOTE:  Specified website \'' + g.args.website_name + '\' is not a valid website on this server.'
        print 'Here\'s a list of websites configured on this server.'
        print
        util.print_websites(g.websites)
        sys.exit(0)

    g.website_directory = g.websites[g.args.website_name]['document_root']

    script_directory = os.path.dirname(os.path.realpath(__file__))

    g.temp_directory = tempfile.mkdtemp(prefix='web_backup_')

    if g.args.message_output_filename is None:
        g.message_output_filename = g.temp_directory + '/messages_' + \
            datetime.datetime.now().strftime('%Y%m%d%H%M%S') + '.log'
    else:
        g.message_output_filename = g.args.message_output_filename

    # Don't do work that'd just get deleted
    if not g.args.post_to_s3 and g.args.delete_zip:
        message_error('Does not make sense to create zip file and delete it without posting to AWS S3. Aborting!')
        util.sys_exit(1)

    # Load AWS creds which are used for checking need for backup and posting backup file
    g.aws_access_key_id = util.get_ini_setting('aws', 'access_key_id', False)
    g.aws_secret_access_key = util.get_ini_setting('aws', 'secret_access_key', False)
    g.aws_region_name = util.get_ini_setting('aws', 'region_name', False)
    if g.args.aws_s3_bucket_name is not None:
        g.aws_s3_bucket_name = g.args.aws_s3_bucket_name
    else:
        g.aws_s3_bucket_name = util.get_ini_setting('aws', 's3_bucket_name', False)

    if g.args.zip_file_password is not None:
        g.zip_file_password = g.args.zip_file_password
    else:
        g.zip_file_password = util.get_ini_setting('zip_file', 'password', False)

    # Call the base directory the name of the website
    website_name = os.path.basename(g.website_directory)

    # Start with assumption no backups to do
    backups_to_do = None

    # If user specified just to show work to be done (backups to do), calculate, display, and exit
    if g.args.show_backups_to_do:
        backups_to_do = get_backups_to_do(website_name)
        if backups_to_do is None:
            message_info('Backups in S3 are already up-to-date. Nothing to do')
            util.sys_exit(0)
        else:
            message_info('There are backups/deletions to do')
            message_info('Backup plan details: ' + str(backups_to_do))
            util.sys_exit(0)

    # See if there are backups to do
    backups_to_do = get_backups_to_do(website_name)

    # If we're posting to S3 and deleting the ZIP file, then utility has been run only for purpose of
    # posting to S3. See if there are posts to be done and exit if not
    if g.args.post_to_s3 and g.args.delete_zip and backups_to_do is None:
        message_info('Backups in S3 are already up-to-date. Nothing to do. Exiting!')
        util.sys_exit(0)

    # Create ZIP file of website files
    output_filename = g.temp_directory + '/files.zip'
    os.chdir(g.website_directory)
    exec_zip_list = ['/usr/bin/zip', '-r', output_filename, '.']
    message_info('Zipping website files directory')
    FNULL = open(os.devnull, 'w')
    exit_status = subprocess.call(exec_zip_list, stdout=FNULL)
    if exit_status == 0:
        message_info('Successfully zipped web directory to ' + output_filename)
    else:
        message_warning('Error running zip. Exit status ' + str(exit_status))

    # Create .sql dump file from website's WordPress database (if applicable)
    wp_config_filename = g.website_directory + '/wp-config.php'
    if os.path.isfile(wp_config_filename):
        output_filename = g.temp_directory + '/database.sql'
        dict_db_info = get_wp_database_defines(wp_config_filename,
            ['DB_NAME', 'DB_USER', 'DB_PASSWORD', 'DB_HOST'])
        message_info('Dumping WordPress MySQL database named ' + dict_db_info['DB_NAME'])
        mysqldump_string = '/bin/mysqldump -h ' + dict_db_info['DB_HOST'] + ' -u ' + dict_db_info['DB_USER'] + \
            ' -p' + dict_db_info['DB_PASSWORD'] + ' ' + dict_db_info['DB_NAME'] + ' --add-drop-table -r ' + \
            output_filename
        try:
            exec_output = subprocess.check_output(mysqldump_string, stderr=subprocess.STDOUT, shell=True)
        except subprocess.CalledProcessError as e:
            print 'mysqldump exited with error status ' + str(e.returncode) + ' and error: ' + e.output
            exit(1)

    # Generate final results output zip filename
    if g.args.output_filename is not None:
        output_filename = g.args.output_filename
    elif g.args.delete_zip:
        # We're deleting it when we're done, so we don't care about its location/name. Grab temp filename
        tmp_file = tempfile.NamedTemporaryFile(prefix='web_backup_', suffix='.zip', delete=False)
        output_filename = tmp_file.name
        tmp_file.close()
        os.remove(output_filename)
        message_info('Temp filename used for final results zip output: ' + output_filename)
    else:
        output_filename = script_directory + '/tmp/web_backup_' + \
            datetime.datetime.now().strftime('%Y%m%d%H%M%S') + '.zip'

    # Zip together results files to create final encrypted zip file
    exec_zip_list = ['/usr/bin/zip', '-P', g.zip_file_password, '-j', '-r', output_filename, g.temp_directory + '/']
    message_info('Zipping results files together')
    exit_status = subprocess.call(exec_zip_list, stdout=FNULL)
    if exit_status == 0:
        message_info('Successfully all results to temporary file ' + output_filename)
    else:
        message_error('Error running zip. Exit status ' + str(exit_status))
        sys.exit(1)

    # Push ZIP file into appropriate schedule folders (daily, weekly, monthly, etc.) and then delete excess
    # backups in each folder
    list_completed_backups = []
    if 'notification_emails' in vars(g.args):
        list_notification_emails = g.args.notification_emails
    else:
        list_notification_emails = None
    if g.args.post_to_s3 and backups_to_do is not None:
        for folder_name in backups_to_do:
            if backups_to_do[folder_name]['do_backup']:
                s3_key = upload_to_s3(website_name, folder_name, output_filename)
                expiry_days = {'daily':1, 'weekly':7, 'monthly':31}[folder_name]
                expiring_url = gen_s3_expiring_url(s3_key, expiry_days)
                message_info('Backup URL ' + expiring_url + ' is valid for ' + str(expiry_days) + ' days')
                list_completed_backups.append([folder_name, expiring_url, expiry_days])
            for item_to_delete in backups_to_do[folder_name]['files_to_delete']:
                delete_from_s3(item_to_delete)
        if list_notification_emails is not None:
            send_email_notification(list_completed_backups, list_notification_emails)

    # If user asked not to retain temp directory, don't delete it!  Else, delete it
    if g.args.retain_temp_directory:
        message_info('Retained temporary output directory ' + g.temp_directory)
    else:
        shutil.rmtree(g.temp_directory)
        message_info('Temporary output directory deleted')

    # If user requested generated zip file be deleted, delete it
    if g.args.delete_zip:
        os.remove(output_filename)
        message_info('Output final results zip file deleted')

    util.sys_exit(0)


def get_wp_database_defines(wp_config_filename, list_match_defines):
    dict_wp_database_defines = {}
    with open(wp_config_filename) as wp_config_file:
        for line in wp_config_file:
            line = line.rstrip()  # remove '\n' at end of line
            matched_define = re.search("define\('(?P<key>[A-Z_]+)',\s*'(?P<value>[a-zA-Z0-9_]+)'\)", line)
            if matched_define and matched_define.group('key') and matched_define.group('value'):
                key = matched_define.group('key')
                if key in list_match_defines:
                    value = matched_define.group('value')
                    dict_wp_database_defines[key] = value
    return dict_wp_database_defines


def upload_to_s3(website_name, folder_name, output_filename):
    global g

    # Cache and reuse exact same S3 filename even if upload_to_s3 called multiple times for daily, weekly, etc.
    if g.reuse_output_filename is None:
        g.reuse_output_filename = datetime.datetime.now().strftime('%Y%m%d%H%M%S') + '.zip'

    s3_key = website_name + '/' + folder_name + '/' + g.reuse_output_filename
    s3 = boto3.resource('s3', aws_access_key_id=g.aws_access_key_id, aws_secret_access_key=g.aws_secret_access_key,
        region_name=g.aws_region_name)
    data = open(output_filename, 'rb')
    bucket = s3.Bucket(g.aws_s3_bucket_name)
    bucket.put_object(Key=s3_key, Body=data)
    message_info('Uploaded to S3: ' + s3_key)
    return s3_key


def gen_s3_expiring_url(s3_key, expiry_days):
    global g

    s3Client = boto3.client('s3', aws_access_key_id=g.aws_access_key_id, aws_secret_access_key=g.aws_secret_access_key,
        region_name=g.aws_region_name)
    url = s3Client.generate_presigned_url('get_object', Params = {'Bucket': g.aws_s3_bucket_name, 'Key': s3_key},
        ExpiresIn = expiry_days * 24 * 60 * 60)
    return url


def delete_from_s3(item_to_delete):
    item_to_delete_key = item_to_delete.key
    item_to_delete.delete()
    message_info('Deleted from S3: ' + item_to_delete_key)


def send_email_notification(list_completed_backups, list_notification_emails):
    global g

    body = ''
    sep = ''
    backup_completed_str = 'Backup(s) completed'
    for completed_backup in list_completed_backups:
        folder_name = completed_backup[0]
        url = completed_backup[1]
        expiry_days = completed_backup[2]
        body = body + sep + 'Completed ' + folder_name + ' backup which is accessible at ' + url + ' for ' + \
            str(expiry_days) + ' days.'
        sep = '\r\n\r\n'
    if g.run_util_errors is not None and len(g.run_util_errors) > 0:
        body = body + sep + 'There were errors running the following utility(s): ' + ', '.join(g.run_util_errors) + \
            '. See messages_xxx.log in backup zip file for details.'
        backup_completed_str = backup_completed_str + ' with errors'
    body += '\r\n\r\nSent from local IP address ' + util.get_ip_address()
    util.send_email(list_notification_emails, backup_completed_str, body)


def get_backups_to_do(website_name):
    global g

    schedules_by_folder_name = {x['folder_name']:x for x in get_schedules_from_ini()}
    s3 = boto3.resource('s3', aws_access_key_id=g.aws_access_key_id, aws_secret_access_key=g.aws_secret_access_key,
        region_name=g.aws_region_name)

    # In S3, folder items end with '/', whereas files do not
    file_items = [item for item in s3.Bucket(g.aws_s3_bucket_name).objects.all() if item.key[-1] != '/']
    files_per_folder_dict = {}
    for file_item in file_items:
        path_sects = file_item.key.split('/')
        if len(path_sects) == 3:
            if path_sects[0] == website_name:
                if path_sects[1] in schedules_by_folder_name:
                    filename = path_sects[2]
                    match = re.match('([0-9]{14})\.zip', filename)
                    if match is not None:
                        valid_date = True
                        try:
                            datetime.datetime.strptime(match.group(1), '%Y%m%d%H%M%S')
                        except:
                            valid_date = False
                        if valid_date:
                            if path_sects[1] not in files_per_folder_dict:
                                files_per_folder_dict[path_sects[1]] = [file_item]
                            else:
                                files_per_folder_dict[path_sects[1]].append(file_item)
                        else:
                            message_info('ZIP file with invalid datetime format...ignoring: ' + file_item.key)
                    else:
                        message_info('Unrecognized file in backup folder...ignoring: ' + file_item.key)
                else:
                    message_info('Unrecognized folder or file in web_backups S3 bucket...ignoring: ' + file_item.key)
            else:
                message_info('Non-matching folder or file in web_backups S3 bucket with long path...ignoring: ' +
                    file_item.key)
        else:
            message_info('Found folder that is not part of this website backup area: ' +
                file_item.key)
    backups_to_post_dict = {}
    for folder_name in schedules_by_folder_name:
        num_files_to_keep = schedules_by_folder_name[folder_name]['num_files_to_keep']
        files_to_delete = []
        do_backup = True
        if folder_name in files_per_folder_dict:
            sorted_by_last_modified_list = sorted(files_per_folder_dict[folder_name], key=lambda x: x.last_modified)
            num_files = len(sorted_by_last_modified_list)
            if schedules_by_folder_name[folder_name]['backup_after_datetime'] < \
               sorted_by_last_modified_list[num_files - 1].last_modified:
                do_backup = False
                message_info(folder_name + ': ' + \
                    str(schedules_by_folder_name[folder_name]['backup_after_datetime']) + ' < ' + \
                    str(sorted_by_last_modified_list[num_files - 1].last_modified) + ', no backup to do')
            else:
                message_info(folder_name + ': ' + \
                    str(schedules_by_folder_name[folder_name]['backup_after_datetime']) + ' > ' + \
                    str(sorted_by_last_modified_list[num_files - 1].last_modified) + ', doing backup')
            # TODO deleted 2 out of weekly, should have deleted 3
            if num_files_to_keep > 0 and num_files >= num_files_to_keep:
                if do_backup:
                    kicker = 1
                else:
                    kicker = 0
                if num_files - num_files_to_keep + kicker > 0:
                    files_to_delete = sorted_by_last_modified_list[0:num_files - num_files_to_keep + kicker]
        if do_backup or len(files_to_delete) > 0:
            backups_to_post_dict[folder_name] = {'do_backup': do_backup, 'files_to_delete': files_to_delete}
    if len(backups_to_post_dict) > 0:
        return backups_to_post_dict
    else:
        return None


def get_schedules_from_ini():
    config_file_path = os.path.dirname(os.path.abspath(__file__)) + '/web_backup.ini'
    config_parser = ConfigParser.ConfigParser()
    config_parser.read(config_file_path)
    schedules = []
    curr_datetime = datetime.datetime.now(pytz.UTC)
    message_info('Current UTC datetime: ' + str(curr_datetime))
    for schedule in config_parser.items('schedules'):
        schedule_parms = schedule[1].split(',')
        if len(schedule_parms) != 3:
            message_error("web_backup.ini [schedules] entry '" + schedule[0] + '=' + schedule[1] + "' is invalid. " \
                "Must contain 3 comma-separated fields. Aborting!")
            util.sys_exit(1)
        folder_name = schedule_parms[0].strip()
        delta_time_string = schedule_parms[1].strip()
        num_files_to_keep_string = schedule_parms[2].strip()
        try:
            num_files_to_keep = int(num_files_to_keep_string)
        except:
            message_error("web_backup.ini [schedules] entry '" + schedule[0] + '=' + schedule[1] + "' is " \
                "invalid. '" + num_files_to_keep_string + "' must be a positive integer")
            util.sys_exit(1)
        if num_files_to_keep < 0:
                message_error("web_backup.ini [schedules] entry '" + schedule[0] + '=' + schedule[1] + "' is " \
                "invalid. Specified a negative number of files to keep")
                util.sys_exit(1)
        backup_after_datetime = now_minus_delta_time(delta_time_string)
        if backup_after_datetime is None:
            message_error("web_backup.ini [schedules] entry '" + schedule[0] + '=' + schedule[1] + "' contains " \
                "an invalid interval between backups '" + delta_time_string + "'. Aborting!")
            util.sys_exit(1)
        schedules.append({'folder_name': folder_name, 'backup_after_datetime': backup_after_datetime,
            'num_files_to_keep': num_files_to_keep})
    return schedules


def now_minus_delta_time(delta_time_string):
    curr_datetime = datetime.datetime.now(pytz.UTC)
    slop = 15 * 60 # 15 minutes of "slop" allowed in determining new backup is needed
    # curr_datetime = datetime.datetime(2016, 1, 7, 10, 52, 23, tzinfo=pytz.UTC)
    match = re.match('([1-9][0-9]*)([smhdwMY])', delta_time_string)
    if match is None:
        return None
    num_units = int(match.group(1))
    unit_char = match.group(2)
    seconds_per_unit = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400, 'w': 604800}
    if unit_char in seconds_per_unit:
        delta_secs = (int(seconds_per_unit[unit_char]) * num_units) - slop
        return curr_datetime - datetime.timedelta(seconds=delta_secs)
    elif unit_char == 'M':
        month = curr_datetime.month - 1 - num_units
        year = int(curr_datetime.year + month / 12)
        month = month % 12 + 1
        day = min(curr_datetime.day, calendar.monthrange(year, month)[1])
        return datetime.datetime(year, month, day, curr_datetime.hour, curr_datetime.minute, curr_datetime.second,
            tzinfo=pytz.UTC) - datetime.timedelta(seconds=slop)
    else: # unit_char == 'Y'
        return datetime.datetime(curr_datetime.year + num_units, curr_datetime.month, curr_datetime.day,
            curr_datetime.hour, curr_datetime.minute, curr_datetime.second, tzinfo=pytz.UTC) - \
            datetime.timedelta(seconds=slop)


def run_util(util_name, second_util_name=None):
    global g

    if util_name == 'attendance' and g.args.all_time:
        all_time_list = [ '--all-time' ]
    else:
        all_time_list = []

    datetime_stamp = datetime.datetime.now().strftime('%Y%m%d%H%M%S')
    util_py = 'get_' + util_name + '.py'
    fullpath_util_py = os.path.dirname(os.path.realpath(__file__)) + '/' + util_py
    if second_util_name is not None:
        output_filename1 = g.temp_directory + '/' + util_name + '_' + datetime_stamp + '.csv'
        output_filename2 = g.temp_directory + '/' + second_util_name + '_' + datetime_stamp + '.csv'
        outputs_list = ['--output-' + util_name + '-filename', output_filename1,
            '--output-' + second_util_name + '-filename', output_filename2]
        message_info('Running ' + util_py + ' with output files ' + output_filename1 + ' and ' + \
            output_filename2)
    else:
        output_filename = g.temp_directory + '/' + util_name + '_' + datetime_stamp + '.csv'
        outputs_list = ['--output-filename', output_filename]
        message_info('Running ' + util_py + ' with output file ' + output_filename)
    exec_list = [fullpath_util_py] + all_time_list + ['--message-output-filename', g.message_output_filename] + \
        outputs_list
    exit_status = subprocess.call(exec_list)
    if exit_status == 0:
        message_info('Successfully ran ' + util_py)
    else:
        message_warning('Error running ' + util_py + '. Exit status ' + str(exit_status))
        g.run_util_errors.append(util_py)


def get_websites():
    websites = {}
    default_site = None
    default_info = get_website_info()
    if default_info is not None and 'default_site' in default_info:
        default_site = default_info['default_site']
    _site_files = glob.glob("/etc/httpd/conf.d/_site_*.conf")
    for _site_file in _site_files:
        m = re.match(".*_site_(?P<website_name>.*)\.conf", _site_file)
        if m is not None:
            website_name = m.group('website_name')
            info = get_website_info(website_name)
            info = augment_wordpress_info(info)
            info = augment_backup_info(info)
            if 'server_name' in info and default_site is not None and info['server_name'] == default_site:
                info['default_site'] = True
            else:
                info['default_site'] = False
            websites[website_name] = info
    return websites


def get_website_info(website_name=None):
    if website_name is None:
        fname = '/etc/httpd/conf.d/_site.conf'
        if not os.path.isfile(fname):
            return None
    else:
        fname = '/etc/httpd/conf.d/_site_' + website_name + '.conf'
    results = {}
    results['website_name'] = website_name
    with open(fname, 'r') as f:
        line = f.readline()
        found_vhost = False
        found_server_name = False
        results['server_name'] = '<undefined>'
        found_document_root = False
        results['document_root'] = '<undefined>'
        found_default_site = False
        while line:
            if not found_vhost:
                m = re.match('\s*<VirtualHost\s+\*:443>\s*', line)
                if m is not None:
                    found_vhost = True
            else:
                if re.match('\s*</VirtualHost>\s*', line) is not None:
                    return results
                if not found_server_name:
                    m = re.match('\s*ServerName\s+(?P<server_name>[^\s]+)\s*', line)
                    if m is not None:
                        found_server_name = True
                        results['server_name'] = m.group('server_name')
                if not found_document_root:
                    m = re.match('\s*DocumentRoot\s+(?P<document_root>[^\s]+)\s*', line)
                    if m is not None:
                        found_document_root = True
                        results['document_root'] = m.group('document_root')
                if not found_default_site:
                    m = re.match('\s*Redirect\s+/\s+https://(?P<default_site>[^/\s]+)/?\s*', line)
                    if m is not None:
                        found_default_site = True
                        results['default_site'] = m.group('default_site')
            line = f.readline()
    return results


def augment_wordpress_info(site_info):
    if 'document_root' not in site_info:
        return site_info
    wp_config_filename = site_info['document_root'] + '/wp-config.php'
    if not os.path.isfile(wp_config_filename):
        return site_info
    with open(wp_config_filename, 'r') as f:
        line = f.readline()
        while line:
            m = re.match('\s*define\(\s*\'DB_NAME\'*,\s*\'(?P<db_name>[^\']+)\'\s*\);\s*', line)
            if m is not None:
                site_info['wordpress_database'] = m.group('db_name')
            m = re.match('\s*define\(\s*\'DB_USER\'*,\s*\'(?P<db_user>[^\']+)\'\s*\);\s*', line)
            if m is not None:
                site_info['wordpress_user'] = m.group('db_user')
            line = f.readline()
    return site_info


def augment_backup_info(site_info):
    if 'website_name' not in site_info:
        return site_info
    website_name = site_info['website_name']
    cmd = subprocess.Popen('crontab -l', shell=True, stdout=subprocess.PIPE)
    for line in cmd.stdout:
        m = re.match('^(?P<minute>[0-9]+)\s+(?P<hour>[0-9]+)[^-]+--website-name\s+' \
            '(?P<website_name>[A-Za-z0-9_]+).*(--notification-emails\s+(?P<notification_emails>[A-Za-z0-9_@\.]+))?.*',
            line)
        if m is not None:
            if m.group('website_name') == site_info['website_name']:
                site_info['backup_hour'] = m.group('hour')
                site_info['backup_minute'] = m.group('minute')
                m = re.match('.*--notification-emails\s+(?P<notification_emails>[A-Za-z0-9_@\.]+).*',
                    line)
                if m is not None:
                    site_info['backup_email'] = m.group('notification_emails')
        
    return site_info


def print_websites(websites):
    print_blank = False
    keys = websites.keys()
    keys.sort()
    for website_name in keys:
        if print_blank:
            print
        else:
            print_blank = True

        print 'Website: ' + website_name

        if 'document_root' in websites[website_name]:
            print '    Directory: ' + websites[website_name]['document_root']

        if 'default_site' in websites[website_name]:
            print '    This is default site on server: ' + str(websites[website_name]['default_site'])

        if 'backup_hour' in websites[website_name]:
            if 'backup_email' in websites[website_name]:
                backup_email = ' (will notify ' + websites[website_name]['backup_email'] + ')'
            else:
                backup_email = ''
            print '    Backups for this site are configured for ' + str(websites[website_name]['backup_hour']) + \
                ':' + str(websites[website_name]['backup_minute']).zfill(2) + backup_email
        else:
            print '    There are no backups configured (via crontab) for this site.'

        if 'wordpress_database' in websites[website_name]:
            if 'wordpress_user' in websites[website_name]:
                wordpress_user = ' (accessed as database user \'' + websites[website_name]['wordpress_user'] + '\')'
            else:
                wordpress_user = ''
            print '    This is a Wordpress site stored in database \'' +\
                websites[website_name]['wordpress_database'] + '\'' + wordpress_user

        else:
            print '    This is a static site.  (Not a Wordpress site.)'


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
