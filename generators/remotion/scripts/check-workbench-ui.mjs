// The chat UI ships browser files directly. It needs no npm install or bundling.
import {execFileSync} from 'node:child_process';
import {readdirSync, readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import {resolve} from 'node:path';
import {createContext, runInContext} from 'node:vm';

const staticRoot = fileURLToPath(new URL('../../../src/vwb/web/static/', import.meta.url));
const modules = readdirSync(staticRoot).filter(name => name.endsWith('.js'));
for (const name of [...modules, 'vendor/htm.mjs']) {
  execFileSync(process.execPath, ['--check', resolve(staticRoot, name)], {stdio: 'inherit'});
}

const globals = {console, setTimeout, clearTimeout};
globals.window = globals;
globals.self = globals;
const context = createContext(globals);
for (const name of ['vendor/react.production.min.js', 'vendor/react-dom.production.min.js', 'vendor/eeva/components/bundle.js']) {
  runInContext(readFileSync(resolve(staticRoot, name), 'utf8'), context, {filename: name, timeout: 5000});
}
for (const name of ['Surface', 'Button', 'Icon', 'Tooltip', 'SolsticeMark', 'UserMessage', 'AgentMessage',
  'Composer', 'MessageBar', 'Tabs', 'TabList', 'Tab', 'TabPanel', 'TextField', 'Checkbox']) {
  if (!globals.Eeva?.[name]) throw new Error(`The shipped Eeva bundle is missing ${name}`);
}
if (globals.React?.version !== '18.3.1' || !globals.ReactDOM?.version.startsWith('18.3.1')) {
  throw new Error('The browser React and ReactDOM versions must match the vendored Eeva runtime');
}
console.log(`Workbench UI: ${modules.length + 1} modules parsed; React, ReactDOM and Eeva loaded without npm.`);
