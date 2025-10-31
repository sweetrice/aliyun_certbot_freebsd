#!/bin/sh
nginx -s reload
# upload certificate files to aliyun CDN
/etc/certbot/venv/bin/python /etc/certbot/uploadcert.py
