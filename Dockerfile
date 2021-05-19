# syntax=docker/dockerfile:1

FROM python:3.8-slim-buster
MAINTAINER dulitz@gmail.com

WORKDIR /app

RUN apt-get update
RUN apt-get install -y git openssh-client

RUN mkdir /var/lib/porter

# the next line causes the Docker cache to be invalidated when git changes
ADD https://api.github.com/repos/dulitz/porter/git/refs/heads/main version.json

RUN cd /home && git clone https://github.com/dulitz/porter

WORKDIR /home/porter

# known_hosts can be collected using ssh-keyscan. if you don't need to use ssh
# for sshproxy or for savant, just touch an empty known_hosts file in your build context.

COPY known_hosts /root/.ssh/known_hosts
RUN chmod 700 /root/.ssh

# cache.json can be created by
#   python tesla.py porter.yml
# if porter.yml contains a tesla: top-level key which contains a user: email address.

COPY cache.json /home/porter

RUN cp porter.yml /var/lib/porter/

RUN pip3 install -r req.txt

CMD [ "python3", "porter.py", "/var/lib/porter/porter.yml" ]
