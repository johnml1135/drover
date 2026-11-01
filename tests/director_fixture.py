"""A controllable fake director adapter: launches nothing, records every call, and can finish a run in-process."""


class Handle:
    def __init__(self):
        self.result = None

    def poll(self):
        return self.result


class ControlledAdapter:
    capabilities = dict(live_offer=False, verified_resume=False, fresh_start=True,
                        enforced_token_cap=True, authenticated_processing=True, secure_handoff=True)

    def __init__(self, service):
        self.service = service
        self.started, self.stops, self.adopted = [], [], []
        self.states, self.handles = {}, {}
        self.before_launch = lambda: None
        self.fail_launch = False
        self.process = False

    def launch(self, spec, credential, guard):
        self.before_launch()
        guard()
        # This executor makes zero model calls, hence enforces the reserved cap.
        identity = self.service.authenticate(credential)
        assert identity['generation'] == spec['generation']
        assert spec['max_tokens'] > 0
        self.started.append(dict(spec))
        self.states[spec['nonce']] = 'alive'
        self.handles[spec['run_id']] = Handle()
        if self.process:
            lease = self.service.acquire(identity['role_id'], identity['session_id'])
            messages = self.service.receive(identity['role_id'])
            self.service.commit(identity['role_id'], lease['lease_token'], {'processed': True},
                                [m['delivery_id'] for m in messages], [], 'fixture-process', release=True,
                                delivery_versions={m['delivery_id']: m['message_version'] for m in messages})
            self.handles[spec['run_id']].result = {'status': 'done', 'usage': {'tokens_in': 0, 'tokens_out': 0}}
            self.states[spec['nonce']] = 'gone'
        if self.fail_launch:
            raise OSError('lost launch response')
        return self.handles[spec['run_id']]

    def probe(self, spec):
        return self.states.get(spec['nonce'], 'unknown')

    def adopt(self, spec):
        self.adopted.append(spec['run_id'])
        return self.handles[spec['run_id']]

    def stop(self, spec):
        self.stops.append((spec['run_id'], spec['nonce']))
        return True  # A request is not proof that the process has ceased.
