# sshproxy.py
#
# ssh proxy for Porter, the Prometheus exporter

import logging, os, prometheus_client, subprocess, threading, time

LOGGER = logging.getLogger('porter.sshproxy')

PROXY_COUNT = prometheus_client.Counter('sshproxy_listens', 'number of ssh instances')
RESTART_COUNT = prometheus_client.Counter('sshproxy_restarts',
                                          'number of ssh instances restarted')
REQUEST_COUNT = prometheus_client.Counter('sshproxy_connections',
                                          'number of uses of sshproxy')

class SSHProxy:
    def __init__(self, config):
        self.rewrites = {}
        self.next_identity_file = 0
        ours = config.get('sshproxy', {})
        if ours:
            keys = ours.get('key', [])
            if type(keys) == type(''):
                keys = [keys]
            self.identityfiles = [self._makeidentityfile(k) for k in keys]
            self.command = ['ssh', '-aknxNT'] + ['-i%s' % f for f in self.identityfiles]
            self.rewrites = {k: tuple(v) for (k, v) in ours.items() if k != 'key'}
        else:
            self.identityfiles = []
        self.proxies, self.zombies = {}, []
        self.proxies_cv = threading.Condition()

    def _makeidentityfile(self, keystring):
        """write keystring (a private key) to an identifyfile in $HOME/porter-ssh-identities/<>
           and return the name of the file"""
        base = '%s/porter-ssh-identities' % os.environ['HOME']
        name = '%s/%d' % (base, self.next_identity_file)
        self.next_identity_file += 1
        try:
            os.mkdir(base, 0o700)
        except FileExistsError:
            pass
        with open(name, mode='wt') as f:
            os.chmod(name, 0o600)
            f.write(keystring)
        return name
            
    def proxyup(self, target, proxyspec):
        """checks whether the proxy is up and if not, brings it up"""
        with self.proxies_cv:
            proxy = self.proxies.get(proxyspec)
            if proxy:
                r = proxy.poll()
                if r is not None:
                    LOGGER.warning(f'sshproxy for {proxyspec} returned {r}; restarting')
                    RESTART_COUNT.inc()
                    proxy = None
            if not proxy:
                (remoteport, userhost, localhostport) = proxyspec
                cmd = self.command + ['-L', '%s:%s:%d' % (localhostport, target, remoteport), userhost]
                LOGGER.info(f'running {" ".join(cmd)}')
                PROXY_COUNT.inc()
                # we redirect stderr to stdout so Docker will log it
                proxy = subprocess.Popen(cmd, stderr=subprocess.STDOUT)
                time.sleep(1) # so ssh can start up
                self.proxies[proxyspec] = proxy
        
    def rewrite(self, target):
        """checks whether target should be proxied. if not, returns target.

        if it should, ensures the proxy is up and returns the target rewritten to the proxy."""
        v = self.rewrites.get(target)
        if v:
            self.proxyup(target, v)
            REQUEST_COUNT.inc()
            # replace() here is a hack for broken Docker that won't let us bind to localhost.
            # instead we can bind to all interfaces and then connect to localhost here.
            return v[2].replace('0.0.0.0', 'localhost')
        return target

    def restart_proxy_for(self, target):
        """restart the proxy for this target because our caller detected a connection failure"""
        proxyspec = self.rewrites.get(target)
        if proxyspec:
            with self.proxies_cv:
                proxy = self.proxies.get(proxyspec)
                if proxy:
                    proxy.kill()
                    LOGGER.warning(f'proxy for {proxyspec} killed due to failures on {target}')
                    self.proxies[proxyspec] = None
                    if proxy.poll() is None:
                        # this thing ought to be dead but we tried to wait for it and failed
                        self.zombies.append(proxy)

    def terminate(self):
        s = sum([1 for z in self.zombies if z.poll() is None])
        if s:
            LOGGER.warning(f'terminate() with {s} unwaited zombie children')
        if not self.rewrites:
            return # no proxies
        with self.proxies_cv:
            for p in self.proxies.values():
                if p.poll() is not None:
                    p.terminate()
