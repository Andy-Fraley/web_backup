#!/usr/bin/env python

import logging
import os
import ConfigParser
import string
import sys
import re
import tempfile
from xml.etree import ElementTree
import socket
import glob
import subprocess


def sys_exit(level=0):
    logging.shutdown()
    sys.exit(level)


def set_logger(message_level='Warning', message_output_filename=None, program_filename=None):
    # Set logging level
    if message_level is not None:
        if message_level not in ['Info', 'Warning', 'Error']:
            logging.error("Specified message level '" + str(message_level) +
                "' must be 'Info', 'Warning', or 'Error'")
            sys.exit(1)
    else:
        message_level = 'Warning'
    logging_map = {
        'Info': logging.INFO,
        'Warning': logging.WARNING,
        'Error': logging.ERROR
    }
    logging.getLogger().setLevel(logging_map[message_level])

    # Set output filename (or leave as stderr) and format
    if message_output_filename is not None:
        if program_filename is not None:
            if program_filename[-3:] == '.py':
                program_filename = program_filename[:-3]
            logging.basicConfig(filename=message_output_filename, format='%(asctime)s:%(levelname)s:' +
                program_filename + ':%(message)s', datefmt='%Y-%m-%d %H:%M:%S')
        else:
            logging.basicConfig(filename=message_output_filename, format='%(asctime)s:%(levelname)s:%(message)s',
                datefmt='%Y-%m-%d %H:%M:%S')
    else:
        logging.basicConfig(format='%(asctime)s:%(levelname)s:%(message)s',
            datefmt='%Y-%m-%d %H:%M:%S')


def test_write(filename):
    try:
        test_file_write = open(filename, 'wb')
    except:
        logging.error("Cannot write to file '" + filename + "'")
        sys.exit(1)
    else:
        test_file_write.close()
        os.remove(filename)


def get_ini_setting(section, option, none_allowable=True):
    config_file_path = os.path.abspath(os.path.dirname(os.path.abspath(__file__)) + '/../web_backup.ini')
    if not os.path.isfile(config_file_path):
        logging.error("Required ini file '" + config_file_path + "' is missing. Clone file 'web_backup__sample.ini' " +
            "to create file 'web_backup.ini'")
        sys.exit(1)
    config_parser = ConfigParser.ConfigParser()
    config_parser.read(config_file_path)
    try:
        ret_val = config_parser.get(section, option).strip()
    except:
        ret_val = None
    if ret_val == '':
        ret_val = None
    if not none_allowable and ret_val == None:
        logging.error("Required setting in web_backup.ini '[" + section + ']' + option + "' cannot be missing " +
            "or blank")
        sys.exit(1)
    return ret_val


def send_email(recipient, subject, body):
    import smtplib

    gmail_user = get_ini_setting('notification_emails', 'gmail_user')
    if gmail_user is not None:
        gmail_password = get_ini_setting('notification_emails', 'gmail_password')
        if gmail_password is None:
            return

    FROM = gmail_user
    TO = recipient if type(recipient) is list else [recipient]
    SUBJECT = subject
    TEXT = body

    # Prepare actual message
    message = """\From: %s\nTo: %s\nSubject: %s\n\n%s
    """ % (FROM, ", ".join(TO), SUBJECT, TEXT)
    server_ssl = smtplib.SMTP_SSL("smtp.gmail.com", 465)
    server_ssl.ehlo() # optional, called by login()
    server_ssl.login(gmail_user, gmail_password)
    server_ssl.sendmail(FROM, TO, message)
    server_ssl.close()


def get_ip_address():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect(("8.8.8.8", 80))
    return s.getsockname()[0]


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

        if 'server_name' in websites[website_name]:
            print '    Full domain: ' + websites[website_name]['server_name']

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
