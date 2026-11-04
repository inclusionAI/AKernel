#!/usr/bin/env python3
# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0
"""Offline bootstrap boundary/lifecycle tests; host commands and API are mocked."""
import ast
import base64
import copy
import gzip
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

MODULE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('node_image_pull', MODULE / 'node-image-pull.py')
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)
API_METHOD = bootstrap.Bootstrap.api
REGISTRY = 'registry.example.aliyuncs.com'
DOMAIN = 'dragonfly-seed-client.dragonfly-system.svc.cluster.local'
RESTART = ['systemctl','restart','kubelet','--job-mode=fail']
JOB = ['systemctl','show','kubelet','--property=Job','--all']


class BootstrapTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.base = self.root / 'stage'
        self.base.mkdir(mode=0o700)
        for name, path in dict(BASE=self.base, CUSTOM=self.root / 'custom', UPSTREAM=self.base / 'upstream',
                               HOST_RESOLV=self.root / 'resolv', INSTANCE_ID=self.root / 'instance').items():
            patcher = mock.patch.object(bootstrap, name, path)
            patcher.start(); self.addCleanup(patcher.stop)
        self.original = 'nameserver 192.0.2.53\nnameserver 192.0.2.54\noptions timeout:1 rotate\n'
        bootstrap.HOST_RESOLV.write_text(self.original)
        bootstrap.CUSTOM.write_text('OTHER=keep\nKUBELET_CUSTOMIZED_ARGS="--v=2"\n')
        bootstrap.INSTANCE_ID.write_text('i-new')
        self.config = self.root / 'kubelet.json'
        self.config.write_text(json.dumps(dict(clusterDNS=['10.20.0.10'],resolvConf=str(bootstrap.HOST_RESOLV))))
        self.arguments = ['kubelet', '--config=' + str(self.config)]
        self.profile = dict(cluster_id='cluster-test', region='test-region', registry=REGISTRY,
                            domain=DOMAIN, service_cidr='10.20.0.0/20', helper_sha256='a' * 64, pool_marker='default')
        self.node = dict(metadata=dict(name='new-node', uid='uid-new', resourceVersion='1', labels={
            'ack.aliyun.com':'cluster-test', 'alibabacloud.com/nodepool-id':'np-new',
            bootstrap.PROFILE_LABEL:bootstrap.PROFILE, bootstrap.READY_LABEL:'false', bootstrap.POOL_LABEL:'default'}),
            spec=dict(providerID='test-region.i-new'), status=dict(conditions=[dict(type='Ready',status='True')]))
        self.dns, self.options, self.ignore = '', '', 'no'
        self.job = 'Job='
        self.commands, self.patches, self.dry_runs = [], [], []
        self.cri = dict(main_path='/etc/containerd/config.toml', main_sha256='b'*64,
                        effective_sha256='c'*64, roots=[str(self.root / 'hosts')])
        patchers = [mock.patch.object(bootstrap, 'run', side_effect=self.command),
                    mock.patch.object(bootstrap, 'active_cri', side_effect=lambda:copy.deepcopy(self.cri)),
                    mock.patch.object(bootstrap, 'kubelet_arguments', side_effect=lambda:list(self.arguments)),
                    mock.patch.object(bootstrap.Bootstrap, 'api', autospec=True, side_effect=self.api),
                    mock.patch.object(bootstrap.socket, 'getaddrinfo', return_value=[('origin',)]),
                    mock.patch.object(bootstrap.time, 'sleep')]
        for patcher in patchers:
            patcher.start(); self.addCleanup(patcher.stop)

    def api(self, worker, method='GET', patch=None, dry_run=False):
        worker.identity = dict(node='new-node', server='https://api.example:6443', ca_sha256='d'*64)
        node = copy.deepcopy(self.node)
        for op in patch or []:
            parts = [p.replace('~1','/').replace('~0','~') for p in op['path'].split('/')[1:]]
            parent = node
            for part in parts[:-1]: parent = parent[part]
            if op['op'] == 'test':
                if parent.get(parts[-1]) != op['value']: raise bootstrap.Pending('CAS changed')
            else: parent[parts[-1]] = op['value']
        if patch is not None:
            (self.dry_runs if dry_run else self.patches).append(patch)
            if not dry_run: self.node = copy.deepcopy(node)
        return node

    def command(self, args, timeout=30):
        self.commands.append(args)
        if args == JOB:
            if isinstance(self.job, Exception): raise self.job
            return self.job
        if args == ['systemctl','is-active','NetworkManager']: return 'active'
        if args[:4] == ['ip','-j','route','show']: return '[{"dev":"eth0"}]'
        if args[:3] == ['nmcli','-g','GENERAL.CON-UUID']: return '11111111-1111-1111-1111-111111111111'
        if args[:3] == ['nmcli','-g','ipv4.dns']: return self.dns
        if args[:3] == ['nmcli','-g','ipv4.ignore-auto-dns']: return self.ignore
        if args[:5] == ['nmcli','--escape','no','-g','ipv4.dns-options']: return self.options
        if args == RESTART:
            self.arguments.append('--resolv-conf=' + str(bootstrap.UPSTREAM))
        elif args[:3] == ['nmcli','connection','modify']:
            self.dns = args[args.index('ipv4.dns')+1]; self.ignore = 'yes'
            self.options = args[args.index('ipv4.dns-options')+1].strip()
        elif args[:3] == ['nmcli','device','reapply']:
            bootstrap.HOST_RESOLV.write_text(''.join('nameserver '+ip+'\n' for ip in self.dns.split(',')) +
                                            'options '+self.options.replace(',',' ')+'\n')
        return ''

    def test_compatibility_and_fallback_configuration(self):
        ast.parse((MODULE/'node-image-pull.py').read_text(), feature_version=(3,6))
        for plugin, version, local, allowed in [('io.containerd.grpc.v1.cri',(1,7,27),'',True),
                ('io.containerd.cri.v1.images',(2,3,4),'',True),
                ('io.containerd.cri.v1.images',(2,2,1),'',False),
                ('io.containerd.cri.v1.images',(2,2,1),'use_local_image_pull=true\n',True)]:
            text = '[plugins."'+plugin+'"]\n'+local+'[plugins."'+plugin+'".registry]\nconfig_path="/etc/containerd/cert.d:/etc/containerd/certs.d"\n'
            with self.subTest(plugin=plugin,version=version,local=local):
                if allowed: self.assertEqual(len(bootstrap.hosts_roots(text,version)),2)
                else:
                    with self.assertRaises(RuntimeError): bootstrap.hosts_roots(text,version)
        mirror = bootstrap.mirror_config(REGISTRY,DOMAIN)
        self.assertIn(('server = "https://'+REGISTRY+'"').encode(),mirror)
        self.assertIn(b'capabilities = ["pull"]',mirror)
        self.assertNotIn(b'skip_verify',mirror)
        original = '\n# keep\nKUBELET_CUSTOMIZED_ARGS="--v=3 --resolv-conf /old"\n'
        self.assertEqual(bootstrap.customized_args(bootstrap.customized_args(original).decode()),bootstrap.customized_args(original))
        for invalid in ('nameserver 127.0.0.53\n','nameserver 10.20.0.10\n'):
            with self.assertRaises(RuntimeError): bootstrap.upstream_addresses(invalid,['10.20.0.10'])
        socket_dir = self.root / 'sockets'; socket_dir.mkdir()
        alias = self.root / 'socket-alias'; alias.symlink_to(socket_dir, target_is_directory=True)
        dump = '[grpc]\naddress="'+str(socket_dir/'containerd.sock')+'"\n'
        endpoint = 'unix://'+str(alias/'containerd.sock')
        self.assertEqual(bootstrap.cri_socket(dump,['kubelet','--container-runtime-endpoint='+endpoint]),str(socket_dir/'containerd.sock'))
        with self.assertRaisesRegex(RuntimeError,'does not match'):
            bootstrap.cri_socket(dump,['kubelet','--container-runtime-endpoint=unix://'+str(socket_dir/'other.sock')])

    def test_complete_lifecycle_and_ready_reentry_do_not_wait_for_dragonfly(self):
        worker = bootstrap.Bootstrap(self.profile)
        worker.start()
        self.assertEqual(len(self.dry_runs),1); self.assertEqual(len(self.patches),1)
        self.assertEqual(self.node['metadata']['labels'][bootstrap.READY_LABEL],'true')
        self.assertEqual(bootstrap.UPSTREAM.read_text(),self.original)
        self.assertIn('OTHER=keep',bootstrap.CUSTOM.read_text())
        self.assertNotIn('rotate',bootstrap.dns_options(bootstrap.HOST_RESOLV.read_text()))
        self.assertTrue(all(call.args[0] == REGISTRY for call in bootstrap.socket.getaddrinfo.call_args_list))
        self.commands.clear()
        with mock.patch.object(bootstrap.Bootstrap,'prepare') as prepare, mock.patch.object(bootstrap.Bootstrap,'configure') as configure:
            bootstrap.Bootstrap(self.profile).start(); prepare.assert_not_called(); configure.assert_not_called()
        self.assertFalse(any(x[:2]==['systemctl','restart'] or x[:3]==['nmcli','connection','modify'] for x in self.commands))
        self.assertEqual(len(self.patches),1)

    def test_identity_binding_and_dryrun_fail_before_host_configuration(self):
        for key,value in [('ack.aliyun.com','wrong'),(bootstrap.POOL_LABEL,'wrong'),('node-role','dragonfly-seed')]:
            with self.subTest(key=key):
                old=copy.deepcopy(self.node); self.node['metadata']['labels'][key]=value
                with self.assertRaises(RuntimeError): bootstrap.Bootstrap(self.profile).start()
                self.node=old; self.assertFalse(self.commands)
        bootstrap.Bootstrap(self.profile).guard(); self.node['metadata']['uid']='replacement'
        with self.assertRaisesRegex(RuntimeError,'binding changed'): bootstrap.Bootstrap(self.profile).start()
        self.node['metadata']['uid']='uid-new'
        original = self.api
        def forbidden(worker,method='GET',patch=None,dry_run=False):
            if dry_run: raise RuntimeError('own PATCH denied')
            return original(worker,method,patch,dry_run)
        with mock.patch.object(bootstrap.Bootstrap,'api',autospec=API_METHOD,side_effect=forbidden):
            with self.assertRaises(RuntimeError): bootstrap.Bootstrap(self.profile).start()
        self.assertFalse(bootstrap.UPSTREAM.exists()); self.assertFalse(self.commands)

    def test_partial_configuration_resumes_and_foreign_changes_fail_closed(self):
        original = self.command; attempts=[]
        def failed_restart(args,timeout=30):
            if args==RESTART:
                self.commands.append(args)
                raise bootstrap.Pending('job cancelled')
            return original(args,timeout)
        with mock.patch.object(bootstrap,'run',side_effect=failed_restart):
            with self.assertRaises(bootstrap.Pending): bootstrap.Bootstrap(self.profile).start()
        def timed_out_restart(args,timeout=30):
            if args==RESTART:
                self.commands.append(args)
                self.job='Job=42'
                raise subprocess.TimeoutExpired(args,timeout)
            return original(args,timeout)
        with mock.patch.object(bootstrap,'run',side_effect=timed_out_restart):
            with self.assertRaises(subprocess.TimeoutExpired): bootstrap.Bootstrap(self.profile).start()
        for _ in range(3):
            with self.assertRaises(bootstrap.Pending): bootstrap.Bootstrap(self.profile).start()
        self.assertEqual(self.commands.count(RESTART),2)
        self.job='Job='
        def interrupted(args,timeout=30):
            if args==JOB: self.arguments.append('--resolv-conf='+str(bootstrap.UPSTREAM))
            if args[:3]==['nmcli','device','reapply'] and not attempts:
                attempts.append(1); raise bootstrap.Pending('apply pending')
            return original(args,timeout)
        with mock.patch.object(bootstrap,'run',side_effect=interrupted):
            with self.assertRaises(bootstrap.Pending): bootstrap.Bootstrap(self.profile).start()
            bootstrap.Bootstrap(self.profile).start()
        self.assertEqual(self.commands.count(RESTART),2)
        mirror=self.root/'hosts'/REGISTRY/'hosts.toml'; mirror.write_bytes(b'operator change')
        with self.assertRaises(RuntimeError): bootstrap.Bootstrap(self.profile).start()
        self.assertEqual(mirror.read_bytes(),b'operator change')

    def test_orphan_restart_marker_recovers_after_job_finishes(self):
        # State left by interruption after the old intent write but before restart.
        (self.base/'kubelet-restart.json').write_bytes(bootstrap.canonical(dict(custom_sha256='old')))
        for job in ('Job=37', '', 'unrecognized', bootstrap.Pending('query failed')):
            self.job=job
            for _ in range(3):
                with self.assertRaises(bootstrap.Pending): bootstrap.Bootstrap(self.profile).start()
            self.assertEqual(self.commands.count(RESTART),0)
            self.assertEqual(bootstrap.HOST_RESOLV.read_text(),self.original)
            self.assertFalse(any(x[:3]==['nmcli','connection','modify'] for x in self.commands))
        self.job='Job=0'
        bootstrap.Bootstrap(self.profile).start()
        self.assertEqual(self.commands.count(RESTART),1)
        self.assertEqual(self.node['metadata']['labels'][bootstrap.READY_LABEL],'true')

    def test_label_race_readback_and_retry_are_bounded(self):
        original = self.api
        def stale(worker,method='GET',patch=None,dry_run=False):
            if patch and not dry_run: self.node['metadata']['resourceVersion']='new'
            return original(worker,method,patch,dry_run)
        with mock.patch.object(bootstrap.Bootstrap,'api',autospec=API_METHOD,side_effect=stale):
            with self.assertRaises(bootstrap.Pending): bootstrap.Bootstrap(self.profile).start()
        self.assertEqual(self.node['metadata']['labels'][bootstrap.READY_LABEL],'false')
        with mock.patch.object(bootstrap.time,'monotonic',side_effect=[0,2]):
            with self.assertRaisesRegex(RuntimeError,'bounded'):
                bootstrap.retry(lambda:bootstrap.available(False,'pending'),1)

    def test_original_userdata_child_exit_status_and_async_start(self):
        for standard_rc,post_rc,foreign in ((0,0,None),(0,2,None),(2,0,None),(0,0,'file'),(0,0,'symlink')):
            code=1 if foreign else standard_rc or post_rc
            with self.subTest(standard=standard_rc,post=post_rc,foreign=foreign),tempfile.TemporaryDirectory() as directory:
                root=Path(directory); base=root/'stage'; units=root/'units'; units.mkdir(); bins=root/'bin'; bins.mkdir()
                log=root/'service.log'; stub=bins/'systemctl'
                stub.write_text('#!/bin/bash\nprintf "%s\\n" "$*" >> "$SERVICE_LOG"\n'); stub.chmod(0o700)
                unit=units/'akernel-image-pull.service'
                if foreign=='file':unit.write_text('operator unit')
                if foreign=='symlink':unit.symlink_to(root/'missing')
                standard='#!/bin/bash\nprintf original > "$STANDARD_MARKER"\nexit '+str(standard_rc)+'\n'
                post='#!/bin/bash\nprintf original > "$POST_MARKER"\nexit '+str(post_rc)+'\n'
                bundle=dict(helper='# helper\n',profile={},bootstrap=standard,user_data=post,
                            unit=(MODULE/'node-image-pull.service').read_text())
                encoded=base64.b64encode(gzip.compress(json.dumps(bundle).encode())).decode()
                script=(MODULE/'node-image-pull.sh.tftpl').read_text().replace('${bundle_gzip_b64}',encoded)
                script=script.replace('/etc/akernel/image-pull',str(base)).replace('/etc/systemd/system/',str(units)+'/')
                script=script.replace('/usr/bin/python3 -',sys.executable+' -')
                result=subprocess.run(['bash'],input=script,text=True,capture_output=True,env=dict(os.environ,PATH=str(bins)+os.pathsep+os.environ['PATH'],SERVICE_LOG=str(log),STANDARD_MARKER=str(root/'standard'),POST_MARKER=str(root/'post')),timeout=15)
                self.assertEqual(result.returncode,code,result.stderr)
                self.assertEqual((base/'standard.sh').read_bytes(),standard.encode())
                self.assertEqual((base/'post.sh').read_bytes(),post.encode())
                if foreign:
                    self.assertFalse((root/'standard').exists())
                    self.assertTrue(unit.is_symlink() if foreign=='symlink' else unit.read_text()=='operator unit')
                    self.assertFalse(log.exists())
                    continue
                self.assertTrue((root/'standard').exists())
                self.assertEqual((root/'post').exists(),standard_rc==0)
                self.assertEqual((base/'original-user-data-exit-code').read_text(),str(code)+'\n')
                self.assertEqual(log.exists(),code==0)
                if code==0:self.assertIn('start --no-block',log.read_text())


if __name__=='__main__': unittest.main()
