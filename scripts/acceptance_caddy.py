#!/usr/bin/env python3
"""Real Caddy/native HTTPS contract test with an explicitly synthetic provider."""
import argparse
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import http.client
from soak import ROOT, digest, environment, stop_process
from runtime_evidence import fingerprint
from test_mcp_sdk import Fixture, Handler


class Failure(Exception):
    pass


def require(condition, code):
    if not condition:
        raise Failure(code)


def command(arguments, env=None, timeout=60):
    result = subprocess.run(arguments,env=env,stdin=subprocess.DEVNULL,
                            capture_output=True,timeout=timeout)
    require(result.returncode == 0,'command_failed')
    return result.stdout


def eventually(predicate, timeout=5):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except (OSError,http.client.HTTPException):
            pass
        time.sleep(.025)
    raise Failure('condition_timeout')


class CountingHandler(Handler):
    def do_POST(self):
        with self.server.count_lock:
            self.server.calls += 1
        super().do_POST()


class LocalTLS(http.client.HTTPSConnection):
    """Connect to loopback while verifying the configured DNS identity and SNI."""
    def connect(self):
        raw = socket.create_connection(('127.0.0.1',self.port),self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw,server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def exchange(connection,path,bearer=None,headers=None,payload=None,method=None):
    supplied = {'Accept':'application/json','Connection':'close',**(headers or {})}
    if bearer is not None:
        supplied['Authorization'] = 'Bearer '+bearer
    body = None if payload is None else json.dumps(payload).encode()
    if body is not None:
        supplied['Content-Type'] = 'application/json'
    try:
        connection.request(method or ('POST' if body else 'GET'),path,body,supplied)
        response = connection.getresponse()
        raw = response.read(1024*1024+1)
        require(len(raw) <= 1024*1024,'response_budget')
        return response.status,{key.lower():value for key,value in response.getheaders()},raw
    finally:
        connection.close()


def certificates(openssl,folder,hostname):
    tls = folder/'tls'
    tls.mkdir()
    command([openssl,'req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(folder/'ca.key'),
        '-out',str(folder/'ca.pem'),'-days','1','-subj','/CN=QCWS isolated test CA',
        '-addext','basicConstraints=critical,CA:TRUE','-addext','keyUsage=critical,keyCertSign,cRLSign',
        '-addext','subjectKeyIdentifier=hash'])
    command([openssl,'req','-new','-newkey','rsa:2048','-nodes','-keyout',str(tls/'server.key'),
        '-out',str(folder/'server.csr'),'-subj','/CN='+hostname])
    extensions = folder/'server.ext'
    extensions.write_text('basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n'
        'extendedKeyUsage=serverAuth\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\n'
        'subjectAltName=DNS:'+hostname+'\n',encoding='utf-8')
    command([openssl,'x509','-req','-in',str(folder/'server.csr'),'-CA',str(folder/'ca.pem'),
        '-CAkey',str(folder/'ca.key'),'-CAcreateserial','-out',str(tls/'server.pem'),
        '-days','1','-extfile',str(extensions)])
    return tls,folder/'ca.pem'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--Executable',type=Path,required=True)
    parser.add_argument('--Output',type=Path,required=True)
    parser.add_argument('--OpenSSL',default=shutil.which('openssl'))
    args = parser.parse_args()
    require(sys.platform == 'linux' and args.OpenSSL,'linux_openssl_required')
    executable = args.Executable.resolve(strict=True)
    report = {'passed':False,'provider':'synthetic-searxng','scope':'real-caddy-native-https-http1',
        'executable_sha256':digest(executable),'runtime':fingerprint(executable),
        'compose_sha256':digest(ROOT/'deploy/team/compose.yml'),
        'caddyfile_sha256':digest(ROOT/'deploy/team/Caddyfile'),
        'not_tested':['public-acme','http2-stream-reset','http3','public-search']}
    with tempfile.TemporaryDirectory(prefix='qcws-caddy-') as temporary:
        folder = Path(temporary)
        team = folder/'team'
        shutil.copytree(ROOT/'deploy/team',team)
        fixture = Fixture()
        fixture.calls,fixture.count_lock,fixture.RequestHandlerClass = 0,threading.Lock(),CountingHandler
        fixture_thread = threading.Thread(target=lambda:fixture.serve_forever(poll_interval=.05))
        fixture_thread.start()
        reserved,process,compose,compose_env = [],None,None,None
        project = 'qcws-test-'+secrets.token_hex(8)
        try:
            for _ in range(4):
                reservation = socket.socket()
                reserved.append(reservation)
                reservation.bind(('127.0.0.1',0))
            bp,cp,hp,lp = [sock.getsockname()[1] for sock in reserved]
            hostname,bearer = project+'.test',secrets.token_hex(32)
            authority,origin = f'{hostname}:{hp}','https://client-'+secrets.token_hex(8)+'.test'
            tls,ca = certificates(args.OpenSSL,folder,hostname)
            config = json.loads((ROOT/'config/runtime.json').read_text('utf-8'))
            config['business'] = {'host':'127.0.0.1','port':bp,'allowed_hosts':[authority],'origins':[origin]}
            config['control'] = {'host':'127.0.0.1','port':cp,'allowed_hosts':[f'127.0.0.1:{cp}'],'origins':[]}
            config['clients'] = [{'id':'caddy-contract','token_env':'QCWS_CADDY_TEST_TOKEN'}]
            config['provider'] = {'url':f'http://127.0.0.1:{fixture.server_port}/search','ca_file':'','engines':['fixture']}
            config['cache']['directory'] = str(folder/'cache')
            config_path = folder/'runtime.json'
            config_path.write_text(json.dumps(config),encoding='utf-8')
            caddy_path = team/'Caddyfile'
            caddy = caddy_path.read_text('utf-8')
            replacements = {
                '\tadmin off\n':'\tadmin off\n\tauto_https off\n',
                '{$QCWS_HOST} {\n':'{$QCWS_HOST} {\n\tbind 127.0.0.1\n\ttls /etc/qcws-tls/server.pem /etc/qcws-tls/server.key\n',
                'reverse_proxy 127.0.0.1:8088 {':f'reverse_proxy 127.0.0.1:{bp} {{',
                'http://127.0.0.1:8090 {':f'http://127.0.0.1:{lp} {{'}
            for before,after in replacements.items():
                require(caddy.count(before) == 1,'caddy_template_changed')
                caddy = caddy.replace(before,after,1)
            caddy_path.write_text(caddy,encoding='utf-8')
            report['test_caddyfile_sha256'] = digest(caddy_path)
            override = folder/'compose.test.json'
            override.write_text(json.dumps({'services':{'caddy':{'restart':'no',
                'volumes':[{'type':'bind','source':str(tls),'target':'/etc/qcws-tls','read_only':True}],
                'healthcheck':{'test':['CMD','wget','-q','-T','2','-O','/dev/null',f'http://127.0.0.1:{lp}/health/live'],
                    'interval':'1s','timeout':'3s','retries':20,'start_period':'1s'}}}}),encoding='utf-8')
            dotenv = folder/'empty.env'
            dotenv.write_text('',encoding='utf-8')
            compose_env = {key:value for key,value in environment([]).items() if not key.startswith('COMPOSE_')}
            compose_env.update(QCWS_HOST='https://'+authority,QCWS_ACME_EMAIL='test@example.invalid',
                               QCWS_SEARXNG_PORT=str(fixture.server_port),SEARXNG_SECRET=secrets.token_hex(32))
            compose = ['docker','compose','--project-name',project,'--env-file',str(dotenv),
                       '-f',str(team/'compose.yml'),'-f',str(override)]
            rendered = json.loads(command(compose+['config','--format','json'],env=compose_env))
            report['images'] = {name:value['image'] for name,value in rendered['services'].items()}
            require(all(re.fullmatch(r'.+@sha256:[0-9a-f]{64}',image) for image in report['images'].values()),'unpinned_image')
            require(rendered['services']['caddy']['network_mode'] == 'host','reference_network_changed')
            command(compose+['pull','caddy'],env=compose_env,timeout=240)
            for sock in reserved[:2]:
                sock.close()
            native_env = environment([])
            native_env['QCWS_CADDY_TEST_TOKEN'] = bearer
            process = subprocess.Popen([str(executable),'--ConfigPath',str(config_path)],env=native_env,
                stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
            def local(path='/health/ready'):
                return exchange(http.client.HTTPConnection('127.0.0.1',cp,timeout=.5),path,bearer=bearer)
            eventually(lambda:local('/health/live')[0] == 200,15)
            for sock in reserved[2:]:
                sock.close()
            command(compose+['up','-d','--no-deps','--wait','--wait-timeout','30','caddy'],env=compose_env,timeout=45)
            context = ssl.create_default_context(cafile=str(ca))
            require(context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED,'tls_policy')
            def remote(path='/v1/status',**kwargs):
                return exchange(LocalTLS(hostname,hp,context=context,timeout=3),path,**kwargs)
            for path in ('/health/live','/health/ready','/metrics'):
                require(local(path)[0] == 200 and remote(path,bearer=bearer)[0] == 404,'control_exposed')
            require(remote(bearer=bearer)[0] == 200,'https_status')
            untrusted = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            try:
                exchange(LocalTLS(hostname,hp,context=untrusted,timeout=3),'/v1/status',bearer=bearer)
            except ssl.SSLCertVerificationError:
                pass
            else:
                raise Failure('untrusted_ca_accepted')
            wrong = LocalTLS('wrong-'+hostname,hp,context=context,timeout=3)
            try:
                wrong.connect()
            except ssl.SSLError:
                pass
            else:
                raise Failure('wrong_tls_identity_accepted')
            finally:
                wrong.close()
            payload = {'query':'auth-'+secrets.token_hex(8),'limit':1}
            for credential in (None,secrets.token_hex(32)):
                status,headers,_ = remote('/v1/search',bearer=credential,payload=payload)
                require(status == 401 and headers.get('www-authenticate') == 'Bearer','authentication')
            status,headers,_ = remote('/v1/search',bearer=bearer,payload=payload,headers={'Origin':origin+'.invalid'})
            require(status == 403 and 'access-control-allow-origin' not in headers,'bad_origin')
            require(remote(bearer=bearer,headers={'Host':f'{hostname}:{cp}'})[0] == 403,'host_validation')
            require(fixture.calls == 0,'rejected_request_reached_provider')
            status,headers,_ = remote(bearer=bearer,headers={'Origin':origin})
            require(status == 200 and headers.get('access-control-allow-origin') == origin
                    and 'origin' in headers.get('vary','').lower(),'allowed_origin')
            status,headers,_ = remote('/v1/search',method='OPTIONS',headers={'Origin':origin,
                'Access-Control-Request-Method':'POST','Access-Control-Request-Headers':'authorization,content-type'})
            require(status == 204 and headers.get('access-control-allow-origin') == origin,'allowed_preflight')
            require(remote('/v1/search',method='OPTIONS',headers={'Origin':origin+'.invalid',
                'Access-Control-Request-Method':'POST'})[0] == 403,'bad_preflight')
            status,_,raw = remote('/v1/search',bearer=bearer,payload={'query':'success-'+secrets.token_hex(8),'limit':1})
            value = json.loads(raw)
            require(status == 200 and value['status'] == 'ok' and len(value['results']) == 1,'real_native_search')
            fixture.arm()
            pending = LocalTLS(hostname,hp,context=context,timeout=3)
            try:
                pending.request('POST','/v1/search',body=json.dumps({'query':'mcp-cancel-'+secrets.token_hex(8),'limit':1}),
                    headers={'Authorization':'Bearer '+bearer,'Content-Type':'application/json'})
                require(fixture.entered.wait(3),'cancel_barrier')
                def counts():
                    status,_,raw = local()
                    require(status == 200,'control_during_request')
                    value = json.loads(raw)
                    return value['active_calls'],value['active_upstream']
                eventually(lambda:counts() == (1,1),2)
                started = time.monotonic()
                pending.sock.shutdown(socket.SHUT_RDWR)
                pending.close()
                eventually(lambda:fixture.disconnected.is_set() and counts() == (0,0),2)
                report['cancel_recovery_ms'] = round((time.monotonic()-started)*1000,3)
            finally:
                pending.close()
            require(remote('/v1/search',bearer=bearer,payload={'query':'recovered-'+secrets.token_hex(8),'limit':1})[0] == 200,
                    'capacity_not_recovered')
            require(fingerprint(executable) == report['runtime'],'runtime_changed')
            report['passed'] = True
        except BaseException as exc:
            report['error'] = str(exc) if isinstance(exc,Failure) else type(exc).__name__
        finally:
            for sock in reserved:
                sock.close()
            if compose is not None:
                try:
                    try:
                        command(compose+['down','--volumes','--remove-orphans','--timeout','5'],env=compose_env,timeout=40)
                    except (Failure,subprocess.TimeoutExpired):
                        subprocess.run(compose+['kill','caddy'],env=compose_env,capture_output=True,timeout=20)
                        command(compose+['down','--volumes','--remove-orphans','--timeout','5'],env=compose_env,timeout=40)
                    label = 'label=com.docker.compose.project='+project
                    require(not command(['docker','ps','-aq','--filter',label]).strip(),'container_cleanup')
                    require(not command(['docker','volume','ls','-q','--filter',label]).strip(),'volume_cleanup')
                    report['compose_cleanup'] = True
                except BaseException:
                    report['compose_cleanup'],report['passed'] = False,False
            if process is not None:
                report['native_stop'] = stop_process(process)
                report['passed'] = report['passed'] and report['native_stop']['passed']
            fixture.release.set()
            fixture.shutdown()
            fixture.server_close()
            fixture_thread.join(timeout=3)
            report['fixture_cleanup'] = not fixture_thread.is_alive()
            report['passed'] = report['passed'] and report['fixture_cleanup']
    args.Output.parent.mkdir(parents=True,exist_ok=True)
    args.Output.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
