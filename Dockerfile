FROM ubuntu:20.04

# install python
RUN apt-get update \
  && apt-get install -y python3-pip python3-dev \
  && cd /usr/local/bin \
  && ln -s /usr/bin/python3 python \
  && pip3 install --upgrade pip

# @see https://dev.to/grigorkh/fix-tzdata-hangs-during-docker-image-build-4o9m
ENV TZ=Asia/Novosibirsk
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# install  libs
RUN apt-get -y install git libxslt-dev libxml2-dev python3-lxml python3-crypto

COPY requirements.txt /requirements.txt

# install requirements 
RUN pip3 install -r /requirements.txt && rm /requirements.txt

# copy project
# COPY . /app_src
# WORKDIR /app_src

COPY credentialstore_keygen.py /credentialstore_keygen.py

# rename settings example com
# generate keys
RUN python3 /credentialstore_keygen.py >> /credentials_generated && rm /credentialstore_keygen.py

# RUN cp tapiriik/local_settings.py.example tapiriik/local_settings.py && \
#   python3 credentialstore_keygen.py >> tapiriik/local_settings.py

WORKDIR /app

# RUN rm -rf /app_src

# run server, worker and scheduler
ENTRYPOINT git config --global --add safe.directory /app && \
  cp tapiriik/local_settings.py.example tapiriik/local_settings.py && \
  cat /credentials_generated >> tapiriik/local_settings.py && \
  python3 manage.py runserver 0.0.0.0:8000 && \
  python3 sync_worker.py && \
  python3 sync_scheduler.py