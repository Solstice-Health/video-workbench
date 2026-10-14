"""Behavioral checks for browser coordinate/input helpers; Node stays in Remotion."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node is optional for frontend checks")


def test_browser_input_mapping_and_order():
    source = r"""
import assert from 'node:assert/strict';
globalThis.window = {React: {createElement() {}}, Eeva: {}};
const {fittedFrame, viewportPoint, browserUrl, appendInput} =
  await import('../../src/vwb/web/static/browser-panel.js');
// The rendered video may use device pixels while input uses CSS pixels.
const fit = fittedFrame(800, 800, 1600, 900);
assert.deepEqual(fit, {left:0, top:175, width:800, height:450});
assert.deepEqual(viewportPoint(400, 400, fit, {width:800,height:450}), {x:400,y:225});
assert.equal(viewportPoint(400, 100, fit, {width:800,height:450}), null);
assert.deepEqual(viewportPoint(900, 900, fit, {width:800,height:450}, true), {x:799,y:449});
assert.equal(fittedFrame(0,800,1600,900), null);
assert.equal(fittedFrame(800,800,NaN,900), null);
assert.equal(browserUrl('example.com/docs'), 'https://example.com/docs');
assert.equal(browserUrl('localhost:8876/fixture.html'), 'http://localhost:8876/fixture.html');
assert.equal(browserUrl('about:blank'), 'about:blank');
for (const url of ['javascript:alert(1)', 'file:///tmp/a', 'hello world']) assert.throws(() => browserUrl(url));
const event = (eventType, x) => ({type:'input_mouse',eventType,x,y:12,modifiers:0});
const queue=[];
appendInput(queue,event('mouseMoved',1)); appendInput(queue,event('mouseMoved',2));
appendInput(queue,event('mousePressed',2)); appendInput(queue,event('mouseMoved',3));
appendInput(queue,{type:'input_keyboard',eventType:'keyDown',key:'a'});
appendInput(queue,event('mouseMoved',4)); appendInput(queue,event('mouseReleased',4));
assert.deepEqual(queue.map(e=>e.eventType), ['mouseMoved','mousePressed','mouseMoved','keyDown','mouseMoved','mouseReleased']);
assert.equal(queue[0].x,2);
const wheels=[];
appendInput(wheels,{...event('mouseWheel',0),deltaX:8000,deltaY:-8000});
appendInput(wheels,{...event('mouseWheel',0),deltaX:8000,deltaY:-8000});
assert.equal(wheels.length,1); assert.equal(wheels[0].deltaX,10000); assert.equal(wheels[0].deltaY,-10000);
appendInput(wheels,{...event('mouseWheel',0),deltaX:1,deltaY:1,modifiers:2});
assert.equal(wheels.length,2);
"""
    result = subprocess.run([NODE, "--input-type=module", "-e", source], cwd=ROOT / "generators/remotion",
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
