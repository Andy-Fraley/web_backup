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
