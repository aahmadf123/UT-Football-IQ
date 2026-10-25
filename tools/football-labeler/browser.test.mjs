/** Isolated actual browser + loopback/disk persistence. All labels are protocol-only. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { createInterface } from 'node:readline';
import { readFile, readdir } from 'node:fs/promises';
import { resolve } from 'node:path';

const require=createRequire(new URL('../../frontend/package.json',import.meta.url));
const {chromium}=require('@playwright/test');
test('delivered local viewer saves real browser edits, reviews hints, reloads, isolates passes and exports', {timeout:90000},async()=>{
  const python=process.env.LABELER_TEST_PYTHON||resolve('backend/.venv/Scripts/python.exe');
  const child=spawn(python,['tests_local/labeler_browser_fixture.py'],{windowsHide:true,stdio:['ignore','pipe','pipe']});
  const lines=createInterface({input:child.stdout});
  let stderr='';child.stderr.on('data',data=>{stderr+=String(data);});
  const first=await Promise.race([once(lines,'line').then(([line])=>JSON.parse(line)),once(child,'exit').then(()=>{throw Error(stderr||'Fixture exited before serving.');})]);
  const browser=await chromium.launch({headless:true});
  let page;
  try{
    page=await browser.newPage({viewport:{width:1440,height:1000},acceptDownloads:true});
    const errors=[];page.on('pageerror',cause=>errors.push(cause.message));
    await page.goto(first.url);
    await page.locator('#reviewer').fill('protocol-staff');await page.locator('#reviewer').blur();
    await page.locator('#source').selectOption('protocol-source');
    await page.waitForFunction(()=>document.querySelector('video').readyState>=2);
    await page.locator('#canonical-id').fill('protocol-play');await page.locator('#start').fill('0');await page.locator('#end').fill('3.5');
    await page.getByText('Practice period and observed concepts · optional',{exact:true}).click();
    await page.locator('#practice-period').selectOption('inside_run');await page.locator('#offense-concept').fill('staff-entered protocol note');
    await page.locator('#save-play').click();
    await page.waitForFunction(()=>document.querySelector('#save-state').textContent.includes('Saved to local folder'));
    await page.locator('#load-native-frames').click();
    await page.waitForFunction(()=>document.querySelector('#frame-detail').textContent.includes('Exact steps ready'));
    await page.locator('#next-frame').click();
    await page.waitForFunction(()=>Number(document.querySelector('#clock').value.split(' ')[0])===.1);
    await page.waitForFunction(()=>document.querySelector('#clock').dataset.presented==='true');
    await page.locator('#target').fill('player-1');await page.locator('#shot').fill('protocol-shot');
    await page.locator('#actor-kind').selectOption('player');await page.locator('#actor-side').selectOption('offense');
    const canvas=await page.locator('#boxes').boundingBox();
    await page.mouse.move(canvas.x+canvas.width*.2,canvas.y+canvas.height*.2);await page.mouse.down();await page.mouse.move(canvas.x+canvas.width*.4,canvas.y+canvas.height*.7);await page.mouse.up();
    await page.waitForFunction(()=>document.querySelector('#track-rows').children.length===1);
    await page.getByText('Experimental detection hints · never labels until you accept',{exact:true}).click();
    await page.locator('#next-proposal').click();await page.waitForFunction(()=>Number(document.querySelector('#clock').value.split(' ')[0])===1);
    await page.locator('#target').fill('official-1');await page.locator('#actor-kind').selectOption('referee');await page.locator('#actor-side').selectOption('not_applicable');
    await page.locator('#proposal-choice').selectOption('protocol-hint-1');await page.locator('#accept-proposal').click();
    await page.waitForFunction(()=>document.querySelector('#track-rows').children.length===2);
    await page.waitForFunction(()=>document.querySelector('#save-state').textContent.includes('Saved to local folder'));
    const query='/api/draft?source=protocol-source&reviewer=protocol-staff&pass=primary';
    const saved=await (await page.request.get(first.url+query)).json();
    assert.equal(saved.draft.plays[0].dense_tracks.length,2);assert.equal(saved.draft.plays[0].practice_period,'inside_run');
    assert.equal(saved.draft.plays[0].dense_tracks[1].actor_kind,'referee');assert.equal(saved.draft.plays[0].dense_tracks[1].side,'not_applicable');assert.equal(saved.draft.reviewed,false);
    const disk=[];for(const folder of await readdir(resolve(first.root,'saved'),{withFileTypes:true})){if(folder.isDirectory())disk.push(JSON.parse(await readFile(resolve(first.root,'saved',folder.name,'current.json'),'utf8')));}
    assert.equal(disk[0].revision,saved.revision);
    await page.locator('#actor-side').selectOption('defense');await page.locator('#save-identity').click();assert.match(await page.locator('#error').innerText(),/Referees have no/);
    await page.locator('#pass').selectOption('secondary');await page.waitForFunction(()=>document.querySelector('#save-state').textContent.includes('New reviewer/pass'));
    assert.equal(await page.locator('#plays option').count(),0);
    await page.locator('#pass').selectOption('primary');await page.waitForFunction(()=>document.querySelector('#save-state').textContent.includes('Loaded saved local draft'));
    await page.reload();await page.waitForFunction(()=>document.querySelector('#save-state').textContent.includes('Loaded saved local draft'));
    assert.equal(await page.locator('#plays option').count(),1);
    // Typing an identity without loading it must never relabel cached observations.
    await page.locator('#reviewer').fill('other-unloaded-staff');
    await page.evaluate(()=>window.dispatchEvent(new Event('beforeunload')));
    const contaminated=await page.evaluate(()=>localStorage.getItem('footiq-annotation-draft-v1:protocol-source:other-unloaded-staff:primary'));
    assert.equal(contaminated,null);
    await page.locator('#reviewer').fill('protocol-staff');await page.locator('#reviewer').blur();
    await page.waitForFunction(()=>/Loaded saved local draft|Saved to local folder/.test(document.querySelector('#save-state').textContent));
    const downloadPromise=page.waitForEvent('download');await page.locator('#export-draft').click();const download=await downloadPromise;
    const exported=JSON.parse(await readFile(await download.path(),'utf8'));
    assert.equal(exported.reviewer_id,'protocol-staff');assert.equal(exported.plays[0].dense_tracks.length,2);assert.equal(exported.reviewed,false);
    await page.locator('#draft-file').setInputFiles({name:'foreign.json',mimeType:'application/json',buffer:Buffer.from(JSON.stringify({...exported,source_id:'foreign-source'}))});
    assert.match(await page.locator('#error').innerText(),/does not match/);
    assert.equal(await page.locator('#plays option').count(),1);
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({status:'passed_protocol_only',actual_source_sha256:first.source.media_sha256,play_windows:1,manual_targets:2,unreviewed:true,local_disk_revision:saved.revision}));
  }catch(cause){console.error(JSON.stringify({root:first.root,error:await page?.locator('#error').innerText(),status:await page?.locator('#save-state').innerText(),video:await page?.locator('video').evaluate(v=>({paused:v.paused,seeking:v.seeking,time:v.currentTime,ready:v.readyState,rect:v.getBoundingClientRect().toJSON(),clock:document.querySelector('#clock').outerHTML}))}));await page?.screenshot({path:resolve(first.root,'failure.png')});throw cause;}finally{await browser.close();child.kill();lines.close();}
});

test('double-click file entry explains the working local launch instead of silent dead controls',async()=>{
  const browser=await chromium.launch({headless:true});
  try{const page=await browser.newPage();await page.goto(new URL('./index.html',import.meta.url).href);assert.match(await page.locator('#connection-status').innerText(),/start-labeler.cmd/);assert.match(await page.locator('#error').innerText(),/unavailable from file:/);}finally{await browser.close();}
});
