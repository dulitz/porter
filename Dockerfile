# syntax=docker/dockerfile:1

FROM python:3.8-slim-buster
MAINTAINER dulitz@gmail.com

WORKDIR /app

RUN apt-get update \
    apt-get install -y git

RUN mkdir /home    \
    mkdir /var/lib/porter \
    cd /home       \
    git clone https://github.com/dulitz/porter

WORKDIR /home/porter

COPY porter.yml /var/lib/porter/

RUN pip3 install -r req.txt

CMD [ "python3", "porter.py", "/var/lib/porter/porter.yml" ]
