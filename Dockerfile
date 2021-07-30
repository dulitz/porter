# syntax=docker/dockerfile:1

FROM python:3.8-slim-buster
MAINTAINER dulitz@gmail.com

WORKDIR /app

RUN apt-get update
RUN apt-get install -y git openssh-client

RUN mkdir /var/lib/porter

ADD https://raw.githubusercontent.com/dulitz/porter/main/req.txt /var/lib/porter/req.txt

RUN pip3 install -r /var/lib/porter/req.txt

# the next line causes the Docker cache to be invalidated when git changes
ADD https://api.github.com/repos/dulitz/porter/git/refs/heads/main version.json

RUN cd /home && git clone https://github.com/dulitz/porter

WORKDIR /home/porter

# known_hosts can be collected using ssh-keyscan. If you don't need to use ssh
# for sshproxy or for savant, just touch an empty known_hosts file in your
# build context.

COPY known_hosts /root/.ssh/known_hosts
RUN chmod 700 /root/.ssh

# cache.json can be created by
#   python tesla.py porter.yml
# if porter.yml contains a tesla: top-level key which contains a users: key with
# a list of one or more email addresss. If you don't want to use the tesla
# exporter, just touch an empty cache.json file in your build context.

COPY cache.json /var/lib/porter/cache.json
RUN ln -s /var/lib/porter/cache.json /home/porter/cache.json
RUN cp porter.yml /var/lib/porter/

CMD [ "python3", "porter.py", "/var/lib/porter/porter.yml" ]
