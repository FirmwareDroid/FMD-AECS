FROM python:3.9-slim-buster

# TODO finish implementing this service
RUN mkdir -p /var/www/html/
ADD ./flask.py /var/www/html/
ADD ./requirements.txt /var/www/html/
WORKDIR /var/www/html/
########################################################
# Update installation                                  #
########################################################
RUN apt-get --allow-releaseinfo-change update -y && \
    apt-get -y install --no-install-recommends apt-utils && \
    apt-get -y install tar gzip build-essential git unzip curl wget

########################################################
# Install Python dependencies                          #
########################################################
RUN python3 -m pip install --upgrade pip && pip install -r ./requirements.txt

