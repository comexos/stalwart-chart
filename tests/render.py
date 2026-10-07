"""Render checks: python3 tests/render.py (requires Helm and PyYAML)."""
from pathlib import Path
import json
import subprocess
import tempfile
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def render(values=None, succeeds=True, raw_defaults=False):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'values.yaml'
        settings = dict(values or {})
        # Unrelated scenarios explicitly preserve the BlobStore; test S3 defaults separately.
        if not raw_defaults and 'dataStore' not in settings:
            settings['dataStore'] = {'backend': 'rocksdb'}
        if not raw_defaults and 'blobStore' not in settings:
            settings['blobStore'] = {'backend': 'existing'}
        path.write_text(yaml.safe_dump(settings))
        result = subprocess.run(['helm', 'template', 'stalwart', str(ROOT), '--namespace', 'mail', '-f', str(path)], capture_output=True, text=True)
    if not succeeds:
        assert result.returncode != 0, 'Expected invalid values to be rejected'
        return result.stderr
    assert result.returncode == 0, result.stderr
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def workload(docs):
    return next(doc for doc in docs if doc['kind'] == 'StatefulSet')


def plan_lines(docs):
    planmap = next((doc for doc in docs if doc['kind'] == 'ConfigMap' and doc['metadata']['name'].endswith('-provisioning')), None)
    if planmap is None:
        return []
    return [json.loads(line) for line in planmap['data']['plan.ndjson'].strip().splitlines()]


class ChartChecks(unittest.TestCase):
    def test_monitor_namespace_override_is_rejected(self):
        error = render({'metrics': {'enabled': True, 'serviceMonitor': {'enabled': True, 'namespace': 'monitoring'}}}, succeeds=False)
        self.assertIn('namespace', error)

    def test_security_and_internal_management(self):
        docs = render({'service': {'type': 'LoadBalancer'}})
        pod = workload(docs)['spec']['template']['spec']
        container = pod['containers'][0]
        self.assertFalse(pod['automountServiceAccountToken'])
        self.assertTrue(pod['securityContext']['runAsNonRoot'])
        self.assertNotIn('enabled', pod['securityContext'])
        security = container['securityContext']
        self.assertFalse(security['allowPrivilegeEscalation'])
        self.assertTrue(security['readOnlyRootFilesystem'])
        self.assertEqual(security['capabilities'], {'drop': ['ALL'], 'add': ['NET_BIND_SERVICE']})
        services = {doc['metadata']['name']: doc['spec'] for doc in docs if doc['kind'] == 'Service'}
        self.assertEqual(services['stalwart-management']['type'], 'ClusterIP')
        self.assertNotIn('mgmt', [port['name'] for port in services['stalwart']['ports']])
        self.assertEqual(services['stalwart']['externalTrafficPolicy'], 'Local')
        self.assertEqual(container['startupProbe']['failureThreshold'], 60)

    def test_metadata_preserves_selectors_and_annotation_precedence(self):
        docs = render({'additionalLabels': {'team': 'mail', 'app.kubernetes.io/name': 'wrong'}, 'podLabels': {'zone': 'a', 'app.kubernetes.io/instance': 'wrong'}, 'additionalAnnotations': {'owner': 'global'}, 'service': {'annotations': {'owner': 'service'}}, 'podAnnotations': {'checksum/config': 'wrong', 'owner': 'pod'}})
        statefulset = workload(docs)
        pod = statefulset['spec']['template']
        selector = statefulset['spec']['selector']['matchLabels']
        for key, value in selector.items():
            self.assertEqual(pod['metadata']['labels'][key], value)
        self.assertEqual(pod['metadata']['labels']['team'], 'mail')
        self.assertEqual(pod['metadata']['labels']['zone'], 'a')
        self.assertNotEqual(pod['metadata']['annotations']['checksum/config'], 'wrong')
        for service in [doc for doc in docs if doc['kind'] == 'Service']:
            self.assertEqual(service['spec']['selector'], selector)
        main = next(doc for doc in docs if doc['kind'] == 'Service' and doc['metadata']['name'] == 'stalwart')
        self.assertEqual(main['metadata']['annotations']['owner'], 'service')

    def test_disabled_options_and_external_store(self):
        docs = render({'config': {'@type': 'PostgreSql', 'host': 'db'}, 'replicaCount': 2, 'persistence': {'enabled': False}, 'service': {'enabled': False}, 'serviceAccount': {'create': False, 'name': 'existing'}, 'podSecurityContext': {'enabled': False}, 'containerSecurityContext': {'enabled': False}, 'startupProbe': {'enabled': False}, 'livenessProbe': {'enabled': False}, 'readinessProbe': {'enabled': False}, 'existingSecret': 'credentials'})
        statefulset = workload(docs)
        pod = statefulset['spec']['template']['spec']
        container = pod['containers'][0]
        self.assertEqual(pod['serviceAccountName'], 'existing')
        self.assertNotIn('ServiceAccount', [doc['kind'] for doc in docs])
        self.assertNotIn('volumeClaimTemplates', statefulset['spec'])
        self.assertIn({'name': 'data', 'emptyDir': {}}, pod['volumes'])
        self.assertNotIn('securityContext', pod)
        for field in ['securityContext', 'startupProbe', 'livenessProbe', 'readinessProbe']:
            self.assertNotIn(field, container)
        self.assertEqual(container['envFrom'], [{'secretRef': {'name': 'credentials'}}])

    def test_recovery_ports_and_pull_secrets(self):
        docs = render({'recoveryMode': {'enabled': True, 'port': 9090}, 'global': {'imageRegistry': 'mirror.example', 'imagePullSecrets': ['registry']}, 'image': {'digest': 'sha256:abc', 'pullSecrets': ['registry']}})
        pod = workload(docs)['spec']['template']['spec']
        container = pod['containers'][0]
        self.assertEqual(container['image'], 'mirror.example/stalwartlabs/stalwart@sha256:abc')
        self.assertEqual(pod['imagePullSecrets'], [{'name': 'registry'}])
        self.assertEqual(next(port['containerPort'] for port in container['ports'] if port['name'] == 'mgmt'), 9090)
        self.assertEqual(next(doc for doc in docs if doc['kind'] == 'Service' and doc['metadata']['name'].endswith('-headless'))['spec']['ports'][0]['port'], 9090)

    def test_service_to_container_port_mapping(self):
        docs = render({'service': {'ports': {'smtp': {'port': 25, 'containerPort': 1025, 'protocol': 'TCP'}}}})
        container = workload(docs)['spec']['template']['spec']['containers'][0]
        ports = {port['name']: port for port in container['ports']}
        main = next(doc for doc in docs if doc['kind'] == 'Service' and doc['metadata']['name'] == 'stalwart')
        for port in main['spec']['ports']:
            self.assertIn(port['targetPort'], ports)
            self.assertEqual(port['protocol'], ports[port['targetPort']]['protocol'])
        self.assertEqual(ports['smtp']['containerPort'], 1025)
        self.assertEqual(next(port['port'] for port in main['spec']['ports'] if port['name'] == 'smtp'), 25)
        for values in [
            {'service': {'ports': {'smtp': 25}}},
            {'service': {'ports': {'smtp': {'containerPort': 0}}}},
            {'service': {'ports': {'mgmt': {'port': 8080, 'containerPort': 8080, 'protocol': 'TCP'}}}},
        ]:
            with self.subTest(values=values):
                render(values, succeeds=False)

    def test_domain_derived_ingress_hosts(self):
        docs = render({'domain': 'example.org', 'ingress': {'enabled': True}, 'webmail': {'enabled': True, 'ingress': {'enabled': True}, 'adminPassword': {'value': 'x'}}})
        main = next(doc for doc in docs if doc['kind'] == 'Ingress' and doc['metadata']['name'] == 'stalwart')
        self.assertEqual(main['spec']['rules'][0]['host'], 'mail.example.org')
        webmail = next(doc for doc in docs if doc['kind'] == 'Ingress' and doc['metadata']['name'] == 'stalwart-webmail')
        self.assertEqual(webmail['spec']['rules'][0]['host'], 'webmail.example.org')

        # Explicit hosts still take priority over the domain default.
        docs = render({
            'domain': 'example.org',
            'ingress': {'enabled': True, 'hosts': [{'host': 'custom.other.org', 'paths': [{'path': '/', 'pathType': 'Prefix', 'portName': 'https'}]}]},
            'webmail': {'enabled': True, 'ingress': {'enabled': True, 'host': 'custom-webmail.other.org'}, 'adminPassword': {'value': 'x'}},
        })
        main = next(doc for doc in docs if doc['kind'] == 'Ingress' and doc['metadata']['name'] == 'stalwart')
        self.assertEqual(main['spec']['rules'][0]['host'], 'custom.other.org')
        webmail = next(doc for doc in docs if doc['kind'] == 'Ingress' and doc['metadata']['name'] == 'stalwart-webmail')
        self.assertEqual(webmail['spec']['rules'][0]['host'], 'custom-webmail.other.org')

        for invalid in [
            {'ingress': {'enabled': True}},
            {'webmail': {'enabled': True, 'ingress': {'enabled': True}}},
        ]:
            with self.subTest(values=invalid):
                render(invalid, succeeds=False)

    def test_gateway_routes(self):
        default = render()
        self.assertFalse(any(doc['kind'] in ['HTTPRoute', 'TCPRoute'] for doc in default))
        values = {'service': {'ports': {'http': {'port': 8081}, 'smtp': {'port': 2525, 'containerPort': 1025}}}, 'gatewayAPI': {'httpRoute': {'enabled': True, 'parentRefs': [{'name': 'web', 'namespace': 'infra', 'sectionName': 'https'}], 'hostnames': ['mail.example.org']}, 'tcpRoutes': {'smtp': {'parentRefs': [{'name': 'mail', 'sectionName': 'smtp'}]}}}}
        docs = render(values)
        http = next(doc for doc in docs if doc['kind'] == 'HTTPRoute')
        tcp = next(doc for doc in docs if doc['kind'] == 'TCPRoute')
        self.assertEqual(http['spec']['rules'][0]['backendRefs'][0]['port'], 8081)
        self.assertEqual(http['spec']['parentRefs'][0]['namespace'], 'infra')
        self.assertEqual(http['spec']['hostnames'], ['mail.example.org'])
        self.assertEqual(tcp['spec']['rules'][0]['backendRefs'][0]['port'], 2525)
        self.assertEqual(tcp['apiVersion'], 'gateway.networking.k8s.io/v1')
        values['gatewayAPI']['tcpRouteApiVersion'] = 'gateway.networking.k8s.io/v1alpha2'
        self.assertEqual(next(doc for doc in render(values) if doc['kind'] == 'TCPRoute')['apiVersion'], 'gateway.networking.k8s.io/v1alpha2')
        for invalid in [
            {'gatewayAPI': {'httpRoute': {'enabled': True}}},
            {'gatewayAPI': {'tcpRoutes': {'smtp': {'parentRefs': []}}}},
            {'gatewayAPI': {'tcpRoutes': {'unknown': {'parentRefs': [{'name': 'mail'}]}}}},
            {'service': {'enabled': False}, 'gatewayAPI': {'httpRoute': {'enabled': True, 'parentRefs': [{'name': 'web'}]}}},
        ]:
            with self.subTest(values=invalid):
                render(invalid, succeeds=False)

    def test_webmail_gateway_route(self):
        docs = render({'webmail': {'enabled': True, 'adminPassword': {'value': 'x'}}})
        self.assertFalse(any(doc['kind'] == 'HTTPRoute' for doc in docs))

        docs = render({
            'domain': 'example.org',
            'webmail': {
                'enabled': True,
                'adminPassword': {'value': 'x'},
                'gatewayAPI': {'httpRoute': {'enabled': True, 'parentRefs': [{'name': 'mail-gateway', 'namespace': 'infra'}]}},
            },
        })
        route = next(doc for doc in docs if doc['kind'] == 'HTTPRoute' and doc['metadata']['name'] == 'stalwart-webmail')
        self.assertEqual(route['spec']['hostnames'], ['webmail.example.org'])
        self.assertEqual(route['spec']['parentRefs'][0], {'name': 'mail-gateway', 'namespace': 'infra'})
        self.assertEqual(route['spec']['rules'][0]['backendRefs'][0], {'name': 'stalwart-webmail', 'port': 3000})

        # Explicit hostnames override the domain default.
        docs = render({
            'domain': 'example.org',
            'webmail': {
                'enabled': True,
                'adminPassword': {'value': 'x'},
                'gatewayAPI': {'httpRoute': {'enabled': True, 'parentRefs': [{'name': 'gw'}], 'hostnames': ['custom-webmail.other.org']}},
            },
        })
        route = next(doc for doc in docs if doc['kind'] == 'HTTPRoute')
        self.assertEqual(route['spec']['hostnames'], ['custom-webmail.other.org'])

        for invalid in [
            {'webmail': {'gatewayAPI': {'httpRoute': {'enabled': True, 'parentRefs': [{'name': 'gw'}]}}}},
            {'webmail': {'enabled': True, 'adminPassword': {'value': 'x'}, 'gatewayAPI': {'httpRoute': {'enabled': True}}}},
            {'webmail': {'enabled': True, 'adminPassword': {'value': 'x'}, 'gatewayAPI': {'httpRoute': {'enabled': True, 'parentRefs': [{'name': 'gw'}]}}}},
        ]:
            with self.subTest(values=invalid):
                render(invalid, succeeds=False)

    def test_autoscaling_and_anti_affinity(self):
        # Off by default: no HPA, static replicas, no affinity.
        docs = render()
        self.assertFalse(any(doc['kind'] == 'HorizontalPodAutoscaler' for doc in docs))
        spec = workload(docs)['spec']
        self.assertEqual(spec['replicas'], 1)
        self.assertNotIn('affinity', spec['template']['spec'])

        # Enabled: HPA targets the StatefulSet and replicas is omitted so
        # helm upgrade cannot fight the autoscaler.
        docs = render({'dataStore': {'backend': 'postgresql', 'postgresql': {'host': 'db', 'password': 'x'}}, 'autoscaling': {'enabled': True, 'minReplicas': 2, 'maxReplicas': 6, 'targetMemoryUtilizationPercentage': 70}})
        hpa = next(doc for doc in docs if doc['kind'] == 'HorizontalPodAutoscaler' and doc['metadata']['name'] == 'stalwart')
        self.assertEqual(hpa['spec']['scaleTargetRef'], {'apiVersion': 'apps/v1', 'kind': 'StatefulSet', 'name': 'stalwart'})
        self.assertEqual((hpa['spec']['minReplicas'], hpa['spec']['maxReplicas']), (2, 6))
        self.assertEqual({m['resource']['name'] for m in hpa['spec']['metrics']}, {'cpu', 'memory'})
        self.assertNotIn('replicas', workload(docs)['spec'])

        # Custom metrics replace the convenience targets.
        custom = [{'type': 'Pods', 'pods': {'metric': {'name': 'connections'}, 'target': {'type': 'AverageValue', 'averageValue': '100'}}}]
        docs = render({'dataStore': {'backend': 'postgresql', 'postgresql': {'host': 'db', 'password': 'x'}}, 'autoscaling': {'enabled': True, 'metrics': custom}})
        hpa = next(doc for doc in docs if doc['kind'] == 'HorizontalPodAutoscaler')
        self.assertEqual(hpa['spec']['metrics'], custom)

        # Anti-affinity presets, and explicit affinity wins.
        soft = workload(render({'podAntiAffinityPreset': 'soft'}))['spec']['template']['spec']['affinity']['podAntiAffinity']
        self.assertIn('preferredDuringSchedulingIgnoredDuringExecution', soft)
        hard = workload(render({'podAntiAffinityPreset': 'hard'}))['spec']['template']['spec']['affinity']['podAntiAffinity']
        self.assertEqual(hard['requiredDuringSchedulingIgnoredDuringExecution'][0]['topologyKey'], 'kubernetes.io/hostname')
        explicit = {'nodeAffinity': {'requiredDuringSchedulingIgnoredDuringExecution': {'nodeSelectorTerms': []}}}
        affinity = workload(render({'podAntiAffinityPreset': 'hard', 'affinity': explicit}))['spec']['template']['spec']['affinity']
        self.assertEqual(affinity, explicit)

        for invalid in [
            {'autoscaling': {'enabled': True, 'minReplicas': 5, 'maxReplicas': 2}},
            {'autoscaling': {'enabled': True, 'targetCPUUtilizationPercentage': None}},
            {'autoscaling': {'enabled': True}},  # default rocksdb + maxReplicas 5
            {'autoscaling': {'enabled': True, 'targetCPUUtilizationPercentage': 80}, 'resources': {'requests': {'cpu': None}}, 'dataStore': {'backend': 'postgresql', 'postgresql': {'host': 'db', 'password': 'x'}}},
            {'podAntiAffinityPreset': 'bogus'},
        ]:
            with self.subTest(values=invalid):
                render(invalid, succeeds=False)

    def test_coordinator_provisioning(self):
        base = {'provisioning': {'existingSecret': 'admin'}}

        # Default: untouched, and no Job unless something else needs it.
        self.assertFalse(any(line['object'] == 'Coordinator' for line in plan_lines(render(base))))

        def coordinator(values):
            docs = render({**base, 'coordinator': values})
            return next(line for line in plan_lines(docs) if line['object'] == 'Coordinator'), docs

        # NATS with an existing Secret: Set fields use the object-of-true encoding
        # and the password reaches the server through an env var reference.
        line, docs = coordinator({'type': 'nats', 'nats': {'addresses': ['nats:4222', 'nats2:4222'], 'existingSecret': 'nats-auth', 'passwordSecretKey': 'pw'}})
        self.assertEqual(line['@type'], 'update')
        self.assertEqual(line['value'], {
            '@type': 'Nats',
            'addresses': {'nats:4222': True, 'nats2:4222': True},
            'useTls': False,
            'authUsername': 'stalwart',
            'authSecret': {'@type': 'EnvironmentVariable', 'variableName': 'STALWART_NATS_PASSWORD'},
            'credentials': {'@type': 'None'},
        })
        env = {e['name']: e for e in workload(docs)['spec']['template']['spec']['containers'][0]['env']}
        self.assertEqual(env['STALWART_NATS_PASSWORD']['valueFrom']['secretKeyRef'], {'name': 'nats-auth', 'key': 'pw'})

        # Plain-value fallback goes through the chart-managed Secret.
        line, docs = coordinator({'type': 'nats', 'nats': {'addresses': ['nats:4222'], 'password': 'p'}})
        env = {e['name']: e for e in workload(docs)['spec']['template']['spec']['containers'][0]['env']}
        self.assertEqual(env['STALWART_NATS_PASSWORD']['valueFrom']['secretKeyRef'], {'name': 'stalwart-env', 'key': 'natsPassword'})
        self.assertEqual(next(d for d in docs if d['kind'] == 'Secret' and d['metadata']['name'] == 'stalwart-env')['stringData'], {'natsPassword': 'p'})

        # No credentials: authSecret None, no env var.
        line, docs = coordinator({'type': 'nats', 'nats': {'addresses': ['nats:4222']}})
        self.assertEqual(line['value']['authSecret'], {'@type': 'None'})
        env = [e['name'] for e in workload(docs)['spec']['template']['spec']['containers'][0]['env'] or []]
        self.assertNotIn('STALWART_NATS_PASSWORD', env)

        self.assertEqual(coordinator({'type': 'redis', 'redis': {'url': 'redis://redis:6379'}})[0]['value'], {'@type': 'Redis', 'url': 'redis://redis:6379'})
        self.assertEqual(coordinator({'type': 'kafka', 'kafka': {'brokers': ['k:9092']}})[0]['value'], {'@type': 'Kafka', 'brokers': {'k:9092': True}, 'groupId': 'stalwart'})
        self.assertEqual(coordinator({'type': 'zenoh', 'zenoh': {'config': '{ mode: peer }'}})[0]['value'], {'@type': 'Zenoh', 'config': '{ mode: peer }'})

        for invalid in [
            {'coordinator': {'type': 'nats'}},
            {'coordinator': {'type': 'redis'}},
            {'coordinator': {'type': 'kafka', 'kafka': {'brokers': []}}},
            {'coordinator': {'type': 'zenoh'}},
            {'coordinator': {'type': 'bogus'}},
            {'coordinator': {'type': 'redis', 'redis': {'url': 'redis://r'}}, 'provisioning': {}},  # no admin credential
        ]:
            with self.subTest(values=invalid):
                render(invalid, succeeds=False)

    def test_dual_stack_services(self):
        docs = render()
        main = next(d for d in docs if d['kind'] == 'Service' and d['metadata']['name'] == 'stalwart')
        self.assertNotIn('ipFamilyPolicy', main['spec'])
        self.assertNotIn('ipFamilies', main['spec'])

        docs = render({'service': {'ipFamilyPolicy': 'PreferDualStack', 'ipFamilies': ['IPv4', 'IPv6']}, 'webmail': {'enabled': True, 'service': {'ipFamilyPolicy': 'RequireDualStack', 'ipFamilies': ['IPv6', 'IPv4']}}})
        main = next(d for d in docs if d['kind'] == 'Service' and d['metadata']['name'] == 'stalwart')
        self.assertEqual((main['spec']['ipFamilyPolicy'], main['spec']['ipFamilies']), ('PreferDualStack', ['IPv4', 'IPv6']))
        webmail = next(d for d in docs if d['kind'] == 'Service' and d['metadata']['name'] == 'stalwart-webmail')
        self.assertEqual((webmail['spec']['ipFamilyPolicy'], webmail['spec']['ipFamilies']), ('RequireDualStack', ['IPv6', 'IPv4']))
        # The headless and management Services are left alone.
        for name in ('stalwart-headless', 'stalwart-management'):
            self.assertNotIn('ipFamilyPolicy', next(d for d in docs if d['kind'] == 'Service' and d['metadata']['name'] == name)['spec'])

        for invalid in [
            {'service': {'ipFamilies': ['IPv4', 'IPv6']}},
            {'service': {'ipFamilyPolicy': 'SingleStack', 'ipFamilies': ['IPv4', 'IPv6']}},
            {'service': {'ipFamilyPolicy': 'Bogus'}},
            {'webmail': {'enabled': True, 'service': {'ipFamilies': ['IPv4', 'IPv6']}}},
        ]:
            with self.subTest(values=invalid):
                render(invalid, succeeds=False)

    def test_pod_management_policy(self):
        self.assertEqual(workload(render())['spec']['podManagementPolicy'], 'OrderedReady')
        self.assertEqual(workload(render({'podManagementPolicy': 'Parallel'}))['spec']['podManagementPolicy'], 'Parallel')
        render({'podManagementPolicy': 'Bogus'}, succeeds=False)

    def test_webmail_autoscaling_and_anti_affinity(self):
        docs = render({'webmail': {'enabled': True}})
        self.assertFalse(any(doc['kind'] == 'HorizontalPodAutoscaler' for doc in docs))
        deployment = next(doc for doc in docs if doc['kind'] == 'Deployment')
        self.assertEqual(deployment['spec']['replicas'], 1)

        docs = render({'webmail': {'enabled': True, 'adminPassword': {'value': 'x'}, 'sessionSecret': {'existingSecret': 'webmail-session'}, 'podAntiAffinityPreset': 'soft', 'autoscaling': {'enabled': True, 'maxReplicas': 4}}})
        hpa = next(doc for doc in docs if doc['kind'] == 'HorizontalPodAutoscaler')
        self.assertEqual(hpa['metadata']['name'], 'stalwart-webmail')
        self.assertEqual(hpa['spec']['scaleTargetRef'], {'apiVersion': 'apps/v1', 'kind': 'Deployment', 'name': 'stalwart-webmail'})
        self.assertEqual(hpa['spec']['maxReplicas'], 4)
        deployment = next(doc for doc in docs if doc['kind'] == 'Deployment')
        self.assertNotIn('replicas', deployment['spec'])
        env = {e['name']: e for e in deployment['spec']['template']['spec']['containers'][0]['env']}
        self.assertNotIn('ADMIN_CONFIG_READONLY', env)
        term = deployment['spec']['template']['spec']['affinity']['podAntiAffinity']['preferredDuringSchedulingIgnoredDuringExecution'][0]['podAffinityTerm']
        self.assertEqual(term['labelSelector']['matchLabels']['app.kubernetes.io/component'], 'webmail')

        for invalid in [
            {'webmail': {'autoscaling': {'enabled': True}}},
            {'webmail': {'enabled': True, 'autoscaling': {'enabled': True}}},  # max 5 needs pinned admin password
            {'webmail': {'enabled': True, 'adminPassword': {'value': 'x'}, 'autoscaling': {'enabled': True}, 'persistence': {'enabled': True}}},  # RWO + max 5
            {'webmail': {'enabled': True, 'adminPassword': {'value': 'x'}, 'sessionSecret': {'existingSecret': 'webmail-session'}, 'autoscaling': {'enabled': True, 'minReplicas': 5, 'maxReplicas': 2}}},
        ]:
            with self.subTest(values=invalid):
                render(invalid, succeeds=False)

    def test_backend_selection(self):
        import json
        def config(docs):
            return json.loads(next(doc for doc in docs if doc['kind'] == 'ConfigMap')['data']['config.json'])
        self.assertEqual(config(render()), {'@type': 'RocksDb', 'path': '/var/lib/stalwart'})
        values = {'dataStore': {'backend': 'postgresql', 'postgresql': {'host': 'postgres.db', 'existingSecret': 'database', 'passwordSecretKey': 'password'}}, 'persistence': {'enabled': False}, 'replicaCount': 2}
        docs = render(values)
        data = config(docs)
        self.assertEqual(data['@type'], 'PostgreSql')
        self.assertNotIn('path', data)
        self.assertTrue(data['useTls'])
        env = workload(docs)['spec']['template']['spec']['containers'][0]['env']
        self.assertEqual(env[0]['valueFrom']['secretKeyRef'], {'name': 'database', 'key': 'password'})
        values['config'] = {'@type': 'PostgreSql', 'host': 'custom'}
        self.assertEqual(config(render(values)), values['config'])
        for invalid in [
            {'dataStore': {'backend': 'unknown'}},
            {'dataStore': {'backend': 'postgresql'}},
            {'dataStore': {'backend': 'postgresql', 'postgresql': {'host': 'db'}}},
        ]:
            with self.subTest(values=invalid):
                render(invalid, succeeds=False)

    def test_s3_provisioning(self):
        import json
        self.assertFalse(any(doc['kind'] == 'Job' for doc in render()))
        values = {'blobStore': {'backend': 's3', 's3': {'bucket': 'mail', 'existingSecret': 's3-credentials'}}, 'provisioning': {'existingSecret': 'admin-credentials'}}
        docs = render(values)
        planmap = next(doc for doc in docs if doc['kind'] == 'ConfigMap' and doc['metadata']['name'].endswith('-provisioning'))
        plan = json.loads(planmap['data']['plan.ndjson'])
        self.assertEqual(plan['@type'], 'update')
        self.assertEqual(plan['object'], 'BlobStore')
        self.assertEqual(plan['value']['region'], {'@type': 'EuCentral1'})
        self.assertEqual(plan['value']['secretKey'], {'@type': 'EnvironmentVariable', 'variableName': 'STALWART_S3_SECRET_KEY'})
        job = next(doc for doc in docs if doc['kind'] == 'Job')
        self.assertEqual(job['metadata']['annotations']['helm.sh/hook'], 'post-install,post-upgrade')
        pod = job['spec']['template']
        self.assertFalse(pod['spec']['automountServiceAccountToken'])
        self.assertNotEqual(pod['metadata']['labels']['app.kubernetes.io/name'], workload(docs)['spec']['selector']['matchLabels']['app.kubernetes.io/name'])
        auth = next(env for env in pod['spec']['containers'][0]['env'] if env['name'] == 'STALWART_PASSWORD')
        self.assertEqual(auth['valueFrom']['secretKeyRef'], {'name': 'admin-credentials', 'key': 'password'})
        server = workload(docs)['spec']['template']['spec']['containers'][0]
        for name in ['STALWART_S3_ACCESS_KEY', 'STALWART_S3_SECRET_KEY']:
            self.assertEqual(next(env for env in server['env'] if env['name'] == name)['valueFrom']['secretKeyRef']['name'], 's3-credentials')
        values['blobStore']['s3'].update({'endpoint': 'https://minio.example.org', 'customRegion': 'us-east-1'})
        planmap = next(doc for doc in render(values) if doc['kind'] == 'ConfigMap' and doc['metadata']['name'].endswith('-provisioning'))
        self.assertEqual(json.loads(planmap['data']['plan.ndjson'])['value']['region'], {'@type': 'Custom', 'customEndpoint': 'https://minio.example.org', 'customRegion': 'us-east-1'})
        for invalid in [
            {'blobStore': {'backend': 's3'}},
            {'blobStore': {'backend': 's3', 's3': {'bucket': 'mail', 'existingSecret': 's3'}}},
        ]:
            with self.subTest(values=invalid):
                render(invalid, succeeds=False)

    def test_all_credentials_from_existing_secrets(self):
        docs = render({
            'recoveryAdmin': {'enabled': True, 'existingSecret': 'recovery', 'secretKey': 'credential'},
            'dataStore': {'backend': 'postgresql', 'postgresql': {'host': 'db', 'existingSecret': 'postgres', 'passwordSecretKey': 'db-password'}},
            'blobStore': {'backend': 's3', 's3': {'bucket': 'mail', 'existingSecret': 's3', 'accessKeySecretKey': 'id', 'secretKeySecretKey': 'key'}},
            'provisioning': {'existingSecret': 'provisioning', 'passwordSecretKey': 'admin-password', 'image': {'pullSecrets': ['cli-registry']}},
            'image': {'pullSecrets': ['server-registry']},
            'extraEnvVars': [{'name': 'OTHER_PASSWORD', 'valueFrom': {'secretKeyRef': {'name': 'other', 'key': 'password'}}}],
        })
        self.assertFalse(any(doc['kind'] == 'Secret' for doc in docs))
        server = workload(docs)['spec']['template']['spec']
        env = {entry['name']: entry for entry in server['containers'][0]['env']}
        for name, secret, key in [
            ('STALWART_RECOVERY_ADMIN', 'recovery', 'credential'),
            ('STALWART_DB_PASSWORD', 'postgres', 'db-password'),
            ('STALWART_S3_ACCESS_KEY', 's3', 'id'),
            ('STALWART_S3_SECRET_KEY', 's3', 'key'),
            ('OTHER_PASSWORD', 'other', 'password'),
        ]:
            self.assertEqual(env[name]['valueFrom']['secretKeyRef'], {'name': secret, 'key': key})
        self.assertEqual(server['imagePullSecrets'], [{'name': 'server-registry'}])
        job = next(doc for doc in docs if doc['kind'] == 'Job')['spec']['template']['spec']
        self.assertEqual(job['imagePullSecrets'], [{'name': 'cli-registry'}])
        password = next(entry for entry in job['containers'][0]['env'] if entry['name'] == 'STALWART_PASSWORD')
        self.assertEqual(password['valueFrom']['secretKeyRef'], {'name': 'provisioning', 'key': 'admin-password'})

    def test_default_s3_requires_configuration(self):
        render(succeeds=False, raw_defaults=True)
        docs = render({'dataStore': {'postgresql': {'host': 'db', 'existingSecret': 'postgres'}}, 'blobStore': {'s3': {'bucket': 'mail', 'existingSecret': 's3'}}, 'provisioning': {'existingSecret': 'admin'}}, raw_defaults=True)
        self.assertTrue(any(doc['kind'] == 'Job' for doc in docs))
        import json
        config = json.loads(next(doc for doc in docs if doc['kind'] == 'ConfigMap' and doc['metadata']['name'].endswith('-config'))['data']['config.json'])
        self.assertEqual(config['@type'], 'PostgreSql')
        self.assertNotIn('path', config)
        statefulset = workload(docs)
        self.assertNotIn('volumeClaimTemplates', statefulset['spec'])
        self.assertIn({'name': 'data', 'emptyDir': {}}, statefulset['spec']['template']['spec']['volumes'])

    def test_metrics_monitoring(self):
        import json
        docs = render()
        self.assertFalse(any(doc['kind'] in ['Job', 'ServiceMonitor'] for doc in docs))

        values = {'metrics': {'enabled': True}, 'provisioning': {'existingSecret': 'admin-credentials'}}
        docs = render(values)
        job = next(doc for doc in docs if doc['kind'] == 'Job')
        planmap = next(doc for doc in docs if doc['kind'] == 'ConfigMap' and doc['metadata']['name'].endswith('-provisioning'))
        lines = [json.loads(line) for line in planmap['data']['plan.ndjson'].strip().splitlines()]
        metrics_plan = next(line for line in lines if line['object'] == 'Metrics')
        self.assertEqual(metrics_plan, {'@type': 'update', 'object': 'Metrics', 'value': {'prometheus': {'@type': 'Enabled'}}})
        server = workload(docs)['spec']['template']['spec']['containers'][0]
        self.assertNotIn('STALWART_METRICS_PASSWORD', [env['name'] for env in server['env'] or []])
        self.assertIsNotNone(job)

        values = {'metrics': {'enabled': True, 'auth': {'username': 'prometheus', 'existingSecret': 'metrics-auth'}, 'serviceMonitor': {'enabled': True, 'interval': '15s'}}, 'provisioning': {'existingSecret': 'admin-credentials'}}
        docs = render(values)
        planmap = next(doc for doc in docs if doc['kind'] == 'ConfigMap' and doc['metadata']['name'].endswith('-provisioning'))
        lines = [json.loads(line) for line in planmap['data']['plan.ndjson'].strip().splitlines()]
        metrics_plan = next(line for line in lines if line['object'] == 'Metrics')
        self.assertEqual(metrics_plan['value']['prometheus']['authUsername'], 'prometheus')
        self.assertEqual(metrics_plan['value']['prometheus']['authSecret'], {'@type': 'EnvironmentVariable', 'variableName': 'STALWART_METRICS_PASSWORD'})
        server = workload(docs)['spec']['template']['spec']['containers'][0]
        env = next(entry for entry in server['env'] if entry['name'] == 'STALWART_METRICS_PASSWORD')
        self.assertEqual(env['valueFrom']['secretKeyRef'], {'name': 'metrics-auth', 'key': 'password'})
        monitor = next(doc for doc in docs if doc['kind'] == 'ServiceMonitor')
        self.assertEqual(monitor['metadata']['namespace'], 'mail')
        self.assertEqual(monitor['spec']['selector']['matchLabels']['app.kubernetes.io/component'], 'management')
        self.assertEqual(monitor['spec']['endpoints'][0]['port'], 'mgmt')
        self.assertEqual(monitor['spec']['endpoints'][0]['path'], '/metrics/prometheus')
        self.assertEqual(monitor['spec']['endpoints'][0]['interval'], '15s')
        self.assertEqual(monitor['spec']['endpoints'][0]['basicAuth']['password'], {'name': 'metrics-auth', 'key': 'password'})
        secret = next(doc for doc in docs if doc['kind'] == 'Secret' and doc['metadata']['name'].endswith('-metrics-auth'))
        self.assertEqual(secret['stringData'], {'username': 'prometheus'})
        management_service = next(doc for doc in docs if doc['kind'] == 'Service' and doc['metadata']['name'].endswith('-management'))
        self.assertEqual(management_service['metadata']['labels']['app.kubernetes.io/component'], 'management')

        for invalid in [
            {'metrics': {'serviceMonitor': {'enabled': True}}},
            {'metrics': {'enabled': True, 'auth': {'existingSecret': 'metrics-auth'}}, 'provisioning': {'existingSecret': 'admin'}},
        ]:
            with self.subTest(values=invalid):
                render(invalid, succeeds=False)

    def test_email_encryption_provisioning(self):
        # Default: email.provision is false and nothing else in this test's
        # values triggers the Job (the render() helper defaults blobStore
        # to 'existing' for isolation), so no plan exists at all.
        docs = render({'provisioning': {'existingSecret': 'admin'}})
        self.assertFalse(any(line['object'] == 'Email' for line in plan_lines(docs)))

        docs = render({'provisioning': {'existingSecret': 'admin'}, 'email': {'provision': True}})
        email_line = next(line for line in plan_lines(docs) if line['object'] == 'Email')
        self.assertEqual(email_line, {'@type': 'update', 'object': 'Email', 'value': {'encryptAtRest': True, 'encryptOnAppend': False}})

        # email.provision alone is enough to trigger the Job even with
        # everything else disabled, but still needs admin credentials.
        docs = render({'dataStore': {'backend': 'rocksdb'}, 'blobStore': {'backend': 'existing'}, 'email': {'provision': True, 'encryptOnAppend': True}, 'provisioning': {'password': 'admin-secret'}}, raw_defaults=True)
        self.assertTrue(any(doc['kind'] == 'Job' for doc in docs))
        email_line = next(line for line in plan_lines(docs) if line['object'] == 'Email')
        self.assertEqual(email_line['value'], {'encryptAtRest': True, 'encryptOnAppend': True})

        render({'dataStore': {'backend': 'rocksdb'}, 'blobStore': {'backend': 'existing'}, 'email': {'provision': True}}, succeeds=False, raw_defaults=True)

    def test_oidc_directory_provisioning(self):
        # Disabled by default: no Directory/Authentication lines even
        # when something else already triggers the provisioning Job.
        docs = render({'provisioning': {'existingSecret': 'admin'}})
        self.assertFalse(any(line['object'] in ('Directory', 'Authentication') for line in plan_lines(docs)))

        # oidc.enabled alone declares the Directory but never touches
        # Authentication -- inert until activate is also set.
        docs = render({'provisioning': {'existingSecret': 'admin'}, 'oidc': {'enabled': True, 'issuerUrl': 'https://keycloak.example.org/realms/mail'}})
        lines = plan_lines(docs)
        self.assertFalse(any(line['object'] == 'Authentication' for line in lines))
        directory_line = next(line for line in lines if line['object'] == 'Directory')
        self.assertEqual(directory_line['@type'], 'upsert')
        self.assertEqual(directory_line['matchOn'], ['description'])
        oidc_value = directory_line['value']['#keycloak']
        self.assertEqual(oidc_value['@type'], 'Oidc')
        self.assertEqual(oidc_value['issuerUrl'], 'https://keycloak.example.org/realms/mail')
        self.assertEqual(oidc_value['requireScopes'], {'openid': True, 'email': True})
        self.assertEqual(oidc_value['claimUsername'], 'preferred_username')

        # activate=true also cross-references the declared Directory from
        # the Authentication singleton, and custom name/claims flow through.
        docs = render({
            'provisioning': {'existingSecret': 'admin'},
            'oidc': {
                'enabled': True,
                'activate': True,
                'name': 'my-idp',
                'issuerUrl': 'https://keycloak.example.org/realms/mail',
                'usernameDomain': 'example.org',
                'claimGroups': 'groups',
            },
        })
        lines = plan_lines(docs)
        directory_line = next(line for line in lines if line['object'] == 'Directory')
        self.assertIn('#my-idp', directory_line['value'])
        self.assertEqual(directory_line['value']['#my-idp']['usernameDomain'], 'example.org')
        self.assertEqual(directory_line['value']['#my-idp']['claimGroups'], 'groups')
        auth_line = next(line for line in lines if line['object'] == 'Authentication')
        self.assertEqual(auth_line, {'@type': 'update', 'object': 'Authentication', 'value': {'directoryId': '#my-idp'}})

        for invalid in [
            {'oidc': {'activate': True}},
            {'oidc': {'enabled': True}},
        ]:
            with self.subTest(values=invalid):
                render(invalid, succeeds=False)

    def test_webmail(self):
        # Disabled by default: none of the webmail resources render.
        docs = render()
        self.assertFalse(any(doc['kind'] == 'Deployment' for doc in docs))
        self.assertFalse(any('webmail' in doc['metadata']['name'] for doc in docs))

        # Minimal enable: jmapServerUrl auto-derives from the internal
        # http Service port, no persistence, no OAuth.
        docs = render({'webmail': {'enabled': True}})
        deployment = next(doc for doc in docs if doc['kind'] == 'Deployment')
        self.assertEqual(deployment['metadata']['name'], 'stalwart-webmail')
        self.assertEqual(deployment['spec']['selector']['matchLabels'], {'app.kubernetes.io/name': 'stalwart', 'app.kubernetes.io/instance': 'stalwart', 'app.kubernetes.io/component': 'webmail'})
        container = deployment['spec']['template']['spec']['containers'][0]
        env = {e['name']: e for e in container['env']}
        self.assertEqual(env['JMAP_SERVER_URL']['value'], 'http://stalwart:80')
        self.assertNotIn('SESSION_SECRET', env)
        self.assertNotIn('ADMIN_PASSWORD', env)
        self.assertNotIn('OAUTH_ENABLED', env)
        self.assertEqual(container['livenessProbe']['httpGet'], {'path': '/api/health', 'port': 'http'})
        volumes = {v['name']: v for v in deployment['spec']['template']['spec']['volumes']}
        self.assertEqual(volumes['data'], {'name': 'data', 'emptyDir': {}})
        self.assertFalse(any(doc['kind'] == 'PersistentVolumeClaim' and 'webmail' in doc['metadata']['name'] for doc in docs))
        self.assertFalse(any(doc['kind'] == 'Ingress' and 'webmail' in doc['metadata']['name'] for doc in docs))
        self.assertFalse(any(doc['kind'] == 'Secret' and doc['metadata']['name'].endswith('-webmail-env') for doc in docs))

        # Fuller config: explicit jmapServerUrl, persistence, ingress,
        # session/admin credentials (mixed existingSecret + plain
        # fallback), and OAuth wired to the top-level oidc.issuerUrl.
        docs = render({
            'webmail': {
                'enabled': True,
                'jmapServerUrl': 'https://mail.example.org',
                'persistence': {'enabled': True},
                'ingress': {'enabled': True, 'host': 'webmail.example.org'},
                'sessionSecret': {'value': 'dev-session'},
                'adminPassword': {'existingSecret': 'bulwark-admin', 'secretKey': 'password'},
                'telemetry': {'enabled': False},
                'oauth': {'enabled': True, 'clientId': 'bulwark', 'clientSecret': 'dev-client-secret'},
            },
            'oidc': {'enabled': True, 'issuerUrl': 'https://keycloak.example.org/realms/mail'},
            'provisioning': {'existingSecret': 'admin'},
        })
        deployment = next(doc for doc in docs if doc['kind'] == 'Deployment')
        container = deployment['spec']['template']['spec']['containers'][0]
        env = {e['name']: e for e in container['env']}
        self.assertEqual(env['JMAP_SERVER_URL']['value'], 'https://mail.example.org')
        self.assertEqual(env['BULWARK_TELEMETRY']['value'], 'off')
        self.assertEqual(env['SESSION_SECRET']['valueFrom']['secretKeyRef'], {'name': 'stalwart-webmail-env', 'key': 'sessionSecret'})
        self.assertEqual(env['ADMIN_PASSWORD']['valueFrom']['secretKeyRef'], {'name': 'bulwark-admin', 'key': 'password'})
        self.assertEqual(env['OAUTH_CLIENT_ID']['value'], 'bulwark')
        self.assertEqual(env['OAUTH_CLIENT_SECRET']['valueFrom']['secretKeyRef'], {'name': 'stalwart-webmail-env', 'key': 'oauthClientSecret'})
        self.assertEqual(env['OAUTH_ISSUER_URL']['value'], 'https://keycloak.example.org/realms/mail')
        secret = next(doc for doc in docs if doc['kind'] == 'Secret' and doc['metadata']['name'] == 'stalwart-webmail-env')
        self.assertEqual(secret['stringData'], {'sessionSecret': 'dev-session', 'oauthClientSecret': 'dev-client-secret'})
        pvc = next(doc for doc in docs if doc['kind'] == 'PersistentVolumeClaim' and doc['metadata']['name'] == 'stalwart-webmail-data')
        self.assertEqual(pvc['spec']['resources']['requests']['storage'], '2Gi')
        ingress = next(doc for doc in docs if doc['kind'] == 'Ingress' and doc['metadata']['name'] == 'stalwart-webmail')
        self.assertEqual(ingress['spec']['rules'][0]['host'], 'webmail.example.org')
        self.assertEqual(ingress['spec']['rules'][0]['http']['paths'][0]['backend']['service']['name'], 'stalwart-webmail')

        for invalid in [
            {'webmail': {'enabled': True}, 'service': {'enabled': False}},
            {'webmail': {'ingress': {'enabled': True}}},
            {'webmail': {'enabled': True, 'oauth': {'enabled': True}}},
            {'webmail': {'enabled': True, 'oauth': {'enabled': True, 'clientId': 'x', 'autoSso': True}}},
            {'webmail': {'enabled': True, 'replicaCount': 2, 'persistence': {'enabled': True}}},
            {'webmail': {'enabled': True, 'settingsSync': {'enabled': True}}},
            {'webmail': {'enabled': True, 'replicaCount': 2}},
        ]:
            with self.subTest(values=invalid):
                render(invalid, succeeds=False)

    def test_webmail_multi_replica_session_secret(self):
        for scaling in [{'replicaCount': 2}, {'autoscaling': {'enabled': True, 'maxReplicas': 2}}]:
            for persistence in [{'enabled': False}, {'enabled': True, 'accessMode': 'ReadWriteMany'}]:
                base = {'enabled': True, 'adminPassword': {'value': 'fixed-pw'}, 'persistence': persistence, **scaling}
                with self.subTest(scaling=scaling, persistence=persistence):
                    error = render({'webmail': base}, succeeds=False)
                    self.assertIn('requires webmail.sessionSecret.existingSecret or .value', error)
                    error = render({'webmail': {**base, 'sessionSecret': {'value': 'x' * 31}}}, succeeds=False)
                    self.assertIn('at least 32 characters', error)
                    for secret in [{'value': 'x' * 32}, {'existingSecret': 'shared-session', 'secretKey': 'session-key', 'value': 'ignored'}]:
                        docs = render({'webmail': {**base, 'sessionSecret': secret}})
                        deployment = next(doc for doc in docs if doc['kind'] == 'Deployment')
                        env = {e['name']: e for e in deployment['spec']['template']['spec']['containers'][0]['env']}
                        ref = env['SESSION_SECRET']['valueFrom']['secretKeyRef']
                        if secret.get('existingSecret'):
                            self.assertEqual(ref, {'name': 'shared-session', 'key': 'session-key'})
                        else:
                            managed = next(doc for doc in docs if doc['kind'] == 'Secret' and doc['metadata']['name'] == ref['name'])
                            self.assertEqual(managed['stringData'][ref['key']], secret['value'])

    def test_webmail_multi_replica_without_shared_storage(self):
        # Ephemeral multi-replica: admin config stays writable so the dashboard
        # initialises; shared credentials let every replica accept admin sessions.
        docs = render({'webmail': {'enabled': True, 'replicaCount': 3, 'adminPassword': {'value': 'fixed-pw'}, 'sessionSecret': {'existingSecret': 'webmail-session'}}})
        deployment = next(doc for doc in docs if doc['kind'] == 'Deployment')
        self.assertEqual(deployment['spec']['replicas'], 3)
        env = {e['name']: e for e in deployment['spec']['template']['spec']['containers'][0]['env']}
        self.assertNotIn('ADMIN_CONFIG_READONLY', env)
        volumes = {v['name']: v for v in deployment['spec']['template']['spec']['volumes']}
        self.assertEqual(volumes['data'], {'name': 'data', 'emptyDir': {}})
        self.assertFalse(any(doc['kind'] == 'PersistentVolumeClaim' for doc in docs))

        # Explicit adminConfigReadonly is passed through as set.
        docs = render({'webmail': {'enabled': True, 'adminConfigReadonly': True}})
        deployment = next(doc for doc in docs if doc['kind'] == 'Deployment')
        env = {e['name']: e for e in deployment['spec']['template']['spec']['containers'][0]['env']}
        self.assertEqual(env['ADMIN_CONFIG_READONLY']['value'], 'true')

        # Shared RWX storage: no read-only flag either.
        docs = render({'webmail': {
            'enabled': True,
            'replicaCount': 3,
            'adminPassword': {'value': 'fixed-pw'},
            'sessionSecret': {'existingSecret': 'webmail-session'},
            'persistence': {'enabled': True, 'accessMode': 'ReadWriteMany'},
        }})
        deployment = next(doc for doc in docs if doc['kind'] == 'Deployment')
        env = {e['name']: e for e in deployment['spec']['template']['spec']['containers'][0]['env']}
        self.assertNotIn('ADMIN_CONFIG_READONLY', env)

    def test_credential_value_fallback(self):
        values = {
            'dataStore': {'backend': 'postgresql', 'postgresql': {'host': 'db', 'password': 'pg-secret'}},
            'blobStore': {'backend': 's3', 's3': {'bucket': 'mail', 'accessKey': 'AKIA', 'secretKey': 's3-secret'}},
            'provisioning': {'password': 'admin-secret'},
            'metrics': {'enabled': True, 'auth': {'username': 'prom', 'password': 'metrics-secret'}, 'serviceMonitor': {'enabled': True}},
        }
        docs = render(values, raw_defaults=True)
        managed = next(doc for doc in docs if doc['kind'] == 'Secret' and doc['metadata']['name'].endswith('-env'))
        self.assertEqual(managed['stringData'], {
            'postgresPassword': 'pg-secret',
            's3AccessKey': 'AKIA',
            's3SecretKey': 's3-secret',
            'provisioningPassword': 'admin-secret',
            'metricsPassword': 'metrics-secret',
        })
        server = workload(docs)['spec']['template']['spec']['containers'][0]
        env = {entry['name']: entry for entry in server['env']}
        for name, key in [
            ('STALWART_DB_PASSWORD', 'postgresPassword'),
            ('STALWART_S3_ACCESS_KEY', 's3AccessKey'),
            ('STALWART_S3_SECRET_KEY', 's3SecretKey'),
            ('STALWART_METRICS_PASSWORD', 'metricsPassword'),
        ]:
            self.assertEqual(env[name]['valueFrom']['secretKeyRef'], {'name': 'stalwart-env', 'key': key})
        job = next(doc for doc in docs if doc['kind'] == 'Job')['spec']['template']['spec']
        password = next(entry for entry in job['containers'][0]['env'] if entry['name'] == 'STALWART_PASSWORD')
        self.assertEqual(password['valueFrom']['secretKeyRef'], {'name': 'stalwart-env', 'key': 'provisioningPassword'})
        monitor = next(doc for doc in docs if doc['kind'] == 'ServiceMonitor')
        self.assertEqual(monitor['metadata']['namespace'], 'mail')
        self.assertEqual(monitor['spec']['endpoints'][0]['basicAuth']['password'], {'name': 'stalwart-env', 'key': 'metricsPassword'})

        # existingSecret still takes priority over a plain value when both are set.
        values['dataStore']['postgresql']['existingSecret'] = 'postgres-external'
        docs = render(values, raw_defaults=True)
        server = workload(docs)['spec']['template']['spec']['containers'][0]
        env = {entry['name']: entry for entry in server['env']}
        self.assertEqual(env['STALWART_DB_PASSWORD']['valueFrom']['secretKeyRef']['name'], 'postgres-external')
        managed = next(doc for doc in docs if doc['kind'] == 'Secret' and doc['metadata']['name'].endswith('-env'))
        self.assertNotIn('postgresPassword', managed['stringData'])

        for invalid in [
            {'dataStore': {'backend': 'postgresql', 'postgresql': {'host': 'db'}}, 'blobStore': {'backend': 'existing'}},
            {'dataStore': {'backend': 'rocksdb'}, 'blobStore': {'backend': 's3', 's3': {'bucket': 'mail', 'accessKey': 'AKIA'}}},
            {'dataStore': {'backend': 'rocksdb'}, 'blobStore': {'backend': 'existing'}, 'metrics': {'enabled': True}},
        ]:
            with self.subTest(values=invalid):
                render(invalid, succeeds=False, raw_defaults=True)

    def test_invalid_combinations(self):
        for values in [{'replicaCount': 2}, {'ingress': {'enabled': True}, 'service': {'enabled': False}}, {'livenessProbe': {'periodSeconds': 0}}, {'startupProbe': {'successThreshold': 2}}, {'recoveryAdmin': {'enabled': True}}]:
            with self.subTest(values=values):
                render(values, succeeds=False)


if __name__ == '__main__':
    unittest.main()
