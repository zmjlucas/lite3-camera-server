/* HTTP commands for the Lite3 console. Ownership and UI timers belong to the caller. */
(function (root) {
  'use strict';

  class Lite3DevClient {
    constructor(fetchRequest = root.fetch.bind(root)) {
      this.fetchRequest = fetchRequest;
    }

    async request(action, body, options = {}) {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), options.timeout || 1500);
      const headers = {Accept: 'application/json'};
      if (body !== undefined) headers['Content-Type'] = 'application/json';
      try {
        const response = await this.fetchRequest('/api/' + action, {
          method: body === undefined ? 'GET' : 'POST', headers,
          body: body === undefined ? undefined : JSON.stringify(body),
          cache: 'no-store', redirect: 'error', signal: controller.signal,
          keepalive: Boolean(options.keepalive)
        });
        const protocol = response.headers.get('X-Lite3-Dev-Protocol');
        if (protocol !== '1') throw new Error('Unsupported Lite3 protocol');
        const data = await response.json();
        if (!response.ok) {
          const error = new Error(typeof data?.error === 'string' ? data.error : 'HTTP ' + response.status);
          error.status = response.status;
          throw error;
        }
        if (!data || typeof data !== 'object' || Array.isArray(data)) {
          throw new Error('Server response must be a JSON object');
        }
        return data;
      } finally {
        clearTimeout(timer);
      }
    }

    status() { return this.request('status'); }
    acquire(owner = 'browser-console') { return this.request('control/acquire', {owner}); }
    heartbeat(lease) { return this.request('control/heartbeat', {lease}); }
    release(lease, options) { return this.request('control/release', {lease}, options); }
    estop(options) { return this.request('control/estop', {}, options); }
    reset(revision, options) { return this.request('control/reset', {revision}, options); }
    stand(lease, seq) { return this.request('dog/stand', {lease, seq}); }
    velocity(lease, seq, {vx = 0, vy = 0, yaw = 0, ttl = 0.4} = {}) {
      return this.request('dog/velocity', {lease, seq, vx, vy, yaw, ttl});
    }
    dogAction(lease, seq, {name, speed, duration}) {
      return this.request('dog/action', {lease, seq, name, speed, duration});
    }
    stop(lease, options) { return this.request('dog/stop', lease ? {lease} : {}, options); }
    armJog(lease, seq, {joint, delta, speed}) {
      return this.request('arm/jog', {lease, seq, joint, delta, speed});
    }
    armState() { return this.request('arm/state', {}, {timeout: 6000}); }
    armReconnect() { return this.request('arm/reconnect', {}, {timeout: 6000}); }
    armOperation(id) { return this.request('arm/operation', {id}); }
    armStop(lease, options) { return this.request('arm/stop', lease ? {lease} : {}, options); }
  }

  root.Lite3DevClient = Lite3DevClient;
  if (typeof module !== 'undefined' && module.exports) module.exports = Lite3DevClient;
})(globalThis);
