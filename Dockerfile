# syntax=docker/dockerfile:1

FROM python:3.8-slim-buster
MAINTAINER dulitz@gmail.com

WORKDIR /app

RUN apt-get update
RUN apt-get install -y git

RUN mkdir /var/lib/porter
RUN cd /home && git clone https://github.com/dulitz/porter

WORKDIR /home/porter

RUN cp porter.yml /var/lib/porter/

RUN pip3 install -r req.txt

CMD [ "python3", "porter.py", "/var/lib/porter/porter.yml" ]
