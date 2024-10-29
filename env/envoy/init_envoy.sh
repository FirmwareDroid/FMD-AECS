#!/bin/sh
mkdir -p /var/www/certbot/.well-known
envoy -c /etc/envoy/envoy.yaml
