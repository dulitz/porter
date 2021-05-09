# sshproxy.py
#
# ssh proxy for Porter, the Prometheus exporter

import os, prometheus_client, subprocess, threading, time

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
            self.command = ['ssh', '-aknxNT', '-p%d' % ours.get('sshport', 22)] + ['-i%s' % f for f in self.identityfiles]
            self.rewrites = {k: tuple(v) for (k, v) in ours.items() if k != 'key' and k != 'port'}
            self.proxies = {}
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
                if r:
                    print('proxy for %s returned %s, restarting' % (proxyspec, r))
                    RESTART_COUNT.inc()
                    proxy = None
            if not proxy:
                (remoteport, userhost, localhostport) = proxyspec
                cmd = self.command + ['-L', '%s:%s:%d' % (localhostport, target, remoteport), userhost]
                print('running', ' '.join(cmd))
                PROXY_COUNT.inc()
                proxy = subprocess.Popen(cmd)
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
                    proxy.terminate()
                    print('proxy for %s terminated due to failures on %s' % (proxyspec, target))

    def terminate(self):
        if not self.rewrites:
            return # no proxies
        with self.proxies_cv:
            for p in self.proxies.values():
                if not p.poll():
                    p.terminate()
