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
import shutil
from util import util


# Fake class only for purpose of limiting global namespace to the 'g' object
class g:
    program_filename = None 
    websites = None
    message_output_filename = None


def main(argv):

    parser = argparse.ArgumentParser()
    parser.add_argument('--from-website-backup-file', required=False, help='Input ZIP filename of a website '
        'backup file.')
    parser.add_argument('--from-s3-website-name', required=False, help='Name of website with backup archive in S3.')
    parser.add_argument('--to-website-name', required=False, help='Name of local website to restore into.')
    parser.add_argument('--message-output-filename', required=False, help='Filename of message output file. If ' \
        'unspecified, then messages are written to stderr as well as into the messages_[datetime_stamp].log file ' \
        'that is zipped into the resulting backup file.')
    parser.add_argument('--zip-file-password', required=False, help='If provided, overrides password used to encryt ' \
        'zip file that is created that was specified in web_backup.ini')
    parser.add_argument('--aws-s3-bucket-name', required=False, help='AWS S3 bucket where output backup zip files ' \
        'are stored')
    parser.add_argument('--overwrite-files', action='store_true', help='If specified, if there are existing files ' \
        'in the target website directory, they are overwritten')
    parser.add_argument('--overwrite-database', action='store_true', help='If specified, if there is an existing ' \
        'database with same name as that being restored, it is dropped before replacement created in its place')
    parser.add_argument('--wp-user', required=False, help='If specified, if the restored site is a Wordpress ' \
                        'site, this is the database user used by Wordpress to access the site\'s database')
    parser.add_argument('--wp-user-password', required=False, help='If specified, if the restored site is a ' \
        'Wordpress site, and --wp-user is specified, then this password is used when creating specified wp-user if ' \
        'that user doesn\'t exist in MySQL')

    g.args = parser.parse_args()

    g.program_filename = os.path.basename(__file__)
    if g.program_filename[-3:] == '.py':
        g.program_filename = g.program_filename[:-3]

    message_level = util.get_ini_setting('logging', 'level')

    if g.args.message_output_filename is not None:
        g.message_output_filename = g.args.message_output_filename

    util.set_logger(message_level, g.message_output_filename, os.path.basename(__file__))

    g.websites = util.get_websites()
    if g.args.to_website_name is None or g.args.to_website_name not in g.websites.keys():
        if g.args.to_website_name is None:
            print 'NOTE:  --to-website-name of website to restore into was not specified.'
        else:
            print 'NOTE:  Specified website \'' + g.args.to_website_name + '\' is not a valid website on this server.'
        print 'Here\'s a list of websites configured on this server.'
        print
        util.print_websites(g.websites)
        sys.exit(0)

    if g.args.wp_user is None and g.args.wp_user_password is not None:
        message_error('You cannot specify new wp_user\'s password without specifying the new wp_user to ' \
            'create with that password.')
        sys.exit(1)

    if g.args.from_website_backup_file is None and g.args.from_s3_website_name is None:
        message_error('Must either specify a local backup ZIP file to restore from or an S3 bucket to grab latest ' \
            'backup from.')
        sys.exit(1)

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
                if path_sects[0] == g.args.from_s3_website_name:
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

        backup_zip_file = tempfile.NamedTemporaryFile(prefix='web_restore_', suffix='.zip', delete=False)
        backup_zip_filename = backup_zip_file.name
        backup_zip_file.close()
        os.remove(backup_zip_filename)
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
    website_root = util.get_ini_setting('website', 'root_directory')
    website_dir = website_root + '/' + g.args.to_website_name

    if not os.path.isdir(website_dir):
        message_error(website_dir + ' is not a directory.')
        sys.exit(1)

    # Ensure target directory is empty before restoring files into it
    existing_file_list = os.listdir(website_dir)
    if len(existing_file_list) != 0:
        if not g.args.overwrite_files:
            message_error(website_dir + ' is not empty and --overwrite-files was not specified.  Aborting...')
            sys.exit(1)
        else:
            message_info('Directory ' + website_dir + ' is not empty. Cleaning first.')
            for the_file in existing_file_list:
                file_path = os.path.join(website_dir, the_file)
                if os.path.isfile(file_path):
                    os.unlink(file_path)
                elif os.path.isdir(file_path):
                    shutil.rmtree(file_path)

    # Restore website files
    exec_zip_list = ['/usr/bin/unzip', temp_directory + '/files.zip', '-d', website_dir]
    message_info('Unzipping backed up website files to ' + website_dir)
    FNULL = open(os.devnull, 'w')
    exit_status = subprocess.call(exec_zip_list, stdout=FNULL)

    # Is there a Wordpress database in the backup for us to restore?
    if os.path.isfile(temp_directory + '/database.sql'):
        db_user = util.get_ini_setting('database', 'user', False)
        db_password = util.get_ini_setting('database', 'password', False)
        output_lines = subprocess.check_output("/bin/mysql -u " + db_user + " -p" + db_password + \
            " -e 'show databases;' 2>/dev/null | /bin/grep wp_", shell=True)
        output_lines_list = [elem for elem in output_lines.split("\n") if elem != ""]
        db_name = 'wp_' + g.args.to_website_name
        if db_name in output_lines_list:
            if not g.args.overwrite_database:
                message_error('Database ' + db_name + ' already exists and --overwrite_database was ' \
                    'not specified. Aborting...')
                sys.exit(1)

        # Recreate database from backup's database.sql file
        wp_user = 'wp_user'
        wp_user_password = None
        if g.args.wp_user is not None:
            wp_user = g.args.wp_user
        if g.args.wp_user is not None and g.args.wp_user_password is not None:
            wp_user_password = g.args.wp_user_password
        wrapper_sql_file = create_wrapper_sql_file(db_name, wp_user, wp_user_password, temp_directory)
        output_lines = subprocess.check_output("/bin/mysql -u " + db_user + " -p" + db_password + \
            " < " + temp_directory + "/restore.sql", shell=True)

        # Update wp-config.php file
        wp_config_filename = website_root + '/' + g.args.to_website_name + '/wp-config.php'
        if not os.path.isfile(wp_config_filename):
            message_error('Wordpress config file ' + wp_config_filename + ' does not exist.')
            sys.exit(1)
        with open(wp_config_filename, 'r') as f_in:
            with open(wp_config_filename + 'x', 'w') as f_out:
                in_line = f_in.readline()
                skipping_lines = False
                while in_line:
                    out_line = None
                    m = re.match('\s*define\(\s*\'DB_NAME\'*,\s*\'(?P<db_name>[^\']+)\'\s*\);\s*', in_line)
                    if m is not None:
                        curr_db_name = m.group('db_name')
                        new_db_name = 'wp_' + g.args.to_website_name
                        if curr_db_name != new_db_name:
                            out_line = 'define(\'DB_NAME\', \'' + new_db_name + '\');\n'
                    m = re.match('\s*define\(\s*\'DB_USER\'*,\s*\'(?P<db_user>[^\']+)\'\s*\);\s*', in_line)
                    if m is not None and g.args.wp_user is not None:
                        curr_db_user = m.group('db_user')
                        new_db_user = g.args.wp_user
                        if curr_db_user != new_db_user:
                            out_line = 'define(\'DB_USER\', \'' + new_db_user + '\');\n'
                    m = re.match('\s*define\(\s*\'DB_PASSWORD\'*,\s*\'(?P<db_password>[^\']+)\'\s*\);\s*', in_line)
                    if m is not None and g.args.wp_user is not None and g.args.wp_user_password is not None:
                        curr_db_password = m.group('db_password')
                        new_db_password = g.args.wp_user_password
                        if curr_db_password != new_db_password:
                            out_line = 'define(\'DB_PASSWORD\', \'' + new_db_password + '\');\n'
                    m = re.match('\s*define\(\s*\'AUTH_KEY\'*', in_line)
                    if m is not None:
                        skipping_lines = True
                    m = re.match('\s*define\(\s*\'NONCE_SALT\'*', in_line)
                    if m is not None:
                        skipping_lines = False
                        send_new_random_salt(f_out)
                        in_line = f_in.readline()
                        next
                    if not skipping_lines:
                        if out_line is None:
                            f_out.write(in_line)
                        else:
                            f_out.write(out_line)
                    in_line = f_in.readline()
        os.rename(wp_config_filename + 'x', wp_config_filename)

        # Rename Wordpress URL in database
        output_lines = subprocess.check_output('mysql -u ' + db_user + ' -p' + db_password + \
            ' -e "use wp_' + g.args.to_website_name + ';select option_value from wp_options ' \
            'where option_name = \'siteurl\';"', shell=True)
        output_lines_list = [elem for elem in output_lines.split("\n") if elem != ""]
        current_full_domain = None
        for line in output_lines_list:
            m = re.match('[\s\|]*https://(?P<full_domain>[a-z0-9\.]+)[\s\|]*', line)
            if m is not None:
                current_full_domain = m.group('full_domain')
        new_full_domain = g.websites[g.args.to_website_name]['server_name']
        if current_full_domain is not None and current_full_domain != new_full_domain:
            message_info('Renaming from https://' + current_full_domain + ' to https://' + new_full_domain + \
                ' in Wordpress database')
            output_lines = subprocess.check_output('/usr/local/bin/wp --path=/var/www/' + g.args.to_website_name + \
                ' search-replace "https://' + current_full_domain + '" "https://' + new_full_domain + '" ' \
                '--skip-columns=guid', shell=True)
        else:
            message_info('No need to rename from https://' + current_full_domain + ' to https://' + \
                new_full_domain + ' in Wordpress database...skipping')

        # Update and (re)secure Wordpress after a restore
        message_info('Updating and (re)securing Wordpress')
        try:
            exec_output = subprocess.check_output('/root/bin/update_and_secure_wp ' + website_dir,
                stderr=subprocess.STDOUT, shell=True)
        except subprocess.CalledProcessError as e:
            print '/root/bin/update_and_secure_wp utility exited with error status ' + str(e.returncode) + \
                ' and error: ' + e.output
            sys.exit(1)

    # Cleanup
    shutil.rmtree(temp_directory)
    message_info('Temporary output directory deleted')

    print 'NOTE: If you web_restore\'d a WordPress installation with WordFence installed, you may need to hand edit'
    print '      .htaccess to modify auto_prepend_file to point at proper wordfence-waf.php in restored'
    print '      installation. Also, interfacing with services like Cloudflare, Google Maps, Google Analytics,'
    print '      NewRelic, Pingdom, etc., may need to be (re)configured to properly support this site.'
    print
    print 'Done!'

    sys.exit(0)


def send_new_random_salt(output_file):
    output_lines = subprocess.check_output('/bin/curl https://api.wordpress.org/secret-key/1.1/salt/', shell=True)
    output_lines_list = [elem for elem in output_lines.split("\n") if elem != ""]
    for line in output_lines_list:
        output_file.write(line + '\n')


def create_wrapper_sql_file(db_name, wp_user, wp_user_password, temp_directory):
    wrapper_sql_filename = temp_directory + '/restore.sql'
    with open(wrapper_sql_filename, 'w') as f_out:
        f_out.write('DROP DATABASE IF EXISTS ' + db_name + ';\n')
        f_out.write('CREATE DATABASE ' + db_name + ';\n')
        if wp_user is not None and wp_user_password is not None:
            f_out.write('CREATE USER IF NOT EXISTS \'' + wp_user + '\'@\'localhost\' IDENTIFIED BY \'' + \
                wp_user_password + '\';\n')
        if wp_user is not None:
            f_out.write('GRANT ALL ON ' + db_name + '.* TO \'' + wp_user + '\'@\'localhost\';\n')
        f_out.write('USE ' + db_name + ';\n')
        f_out.write('source ' + temp_directory + '/database.sql;\n')
    return wrapper_sql_filename


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

    # Only echo to stderr if logger is logging to file (and not stderr)
    if g.args.message_output_filename is not None:
        datetime_stamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        print >> sys.stderr, datetime_stamp + ':' + g.program_filename + ':' + level + ':' + s


if __name__ == "__main__":
    main(sys.argv[1:])
