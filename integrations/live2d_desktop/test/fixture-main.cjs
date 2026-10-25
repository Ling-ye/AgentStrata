const {app} = require('electron');
const {startDesktop} = require('../dist/desktop');
const path = require('node:path');
app.setPath('userData', process.env.DESKTOP_SMOKE_DATA);
global.fixture = {calls: [], failStart: false};
const plugins = ['open-llm-vtuber', 'live2d-generator'].map((id, i) => ({id, status: 'ready',
  url: process.env['DESKTOP_SMOKE_URL_' + i], installed_version: i ? '0.3.0' : '0.4.0'}));
startDesktop({initial: {connection: {distro: 'Fixture', command: '/fixture/agentstrata', root: '/fixture/plugins'},
  mode: 'window', topmost: false}, distributions: async () => ['Fixture'],
  backend: () => ({list: async () => plugins,
    start: async id => {global.fixture.calls.push('start:' + id);
      if (global.fixture.failStart) {global.fixture.failStart = false; throw Error('controlled startup failure');}},
    stop: async () => {global.fixture.calls.push('stop');},
    apply: async () => ({backup: 'b'.repeat(32)}),
    restore: async () => {global.fixture.calls.push('restore');},
  })}).catch(error => {console.error(error); app.exit(1);});
