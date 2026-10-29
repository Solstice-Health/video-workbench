import test from 'node:test';
import assert from 'node:assert/strict';
import { validateJobs } from '../browser-validation.mjs';
import { props } from './fixtures.mjs';
const job = () => ({composition:'Scene',renderer:'browser',props:props(true),out:'/tmp/plate.mov'});
test('all compiled styles and shape variants are accepted; canvas2d gates text', () => {
  assert.doesNotThrow(() => validateJobs([job()]));
  const j=job(); j.renderer='canvas2d'; assert.throws(() => validateJobs([j]),/unsupported layer type text/);
  j.props=props(); assert.doesNotThrow(() => validateJobs([j]));
});
test('code, HTML, source paths, CSS URLs, malformed keys and oversized plates fail closed', () => {
  for (const change of [
    j => { j.source='/tmp/arbitrary.js'; },
    j => { j.props.html='<script>alert(1)</script>'; },
    j => { j.props.layers[0].fill='url(https://example.com/x)'; },
    j => { j.props.layers[0].d='<svg onload="alert(1)">'; },
    j => { j.props.layers[0].box=[{f:1,v:[0,0,10,10]},{f:1,v:[1,1,20,20]}]; },
    j => { j.props.width=100000; },
    j => { j.props.__renderer='gsap'; },
    j => { j.props.layers[0].shape='svg'; delete j.props.layers[0].paths; },
    j => { j.stills=[0]; j.stills_dir='/tmp'; j.pid='../escape'; },
    j => { j.stills=[4]; j.stills_dir='/tmp'; j.pid='valid'; },
  ]) { const j=job(); change(j); assert.throws(() => validateJobs([j])); }
});
test('literal HTML-looking copy remains data', () => {const j=job(); j.props.layers[6].text='</script><img src=x onerror=alert(1)>'; assert.doesNotThrow(() => validateJobs([j]));});

test('none is lowercase only and never a gradient stop across alternate renderers', () => {
  for (const renderer of ['browser','gsap','canvas2d']) {
    const j={composition:'Scene',renderer,props:props(),out:'/tmp/plate.mov'};
    for (const value of ['NONE','None','nOnE']) {
      j.props.layers[0].fill=value;
      assert.throws(() => validateJobs([j]), /none must be lowercase/);
    }
    j.props.layers[0].fill='none';
    assert.doesNotThrow(() => validateJobs([j]));
    j.props.layers[0].gradient=['#112233','none'];
    assert.throws(() => validateJobs([j]), /gradient: none is not a color stop; use transparent/);
    j.props.layers[0].gradient=['NONE','#112233'];
    assert.throws(() => validateJobs([j]), /none must be lowercase/);
    j.props.layers[0].gradient=['transparent','#112233'];
    assert.doesNotThrow(() => validateJobs([j]));
  }
});

test('environment pin is optional and must be a lowercase SHA-256 when present', () => {
  for (const value of [null,{},'', 'A'.repeat(64), 'a'.repeat(63)]) {
    const j=job(); j.props.__environment=value;
    assert.throws(() => validateJobs([j]), /__environment/);
  }
  const j=job(); j.props.__environment='a'.repeat(64);
  assert.doesNotThrow(() => validateJobs([j]));
});
