// A separate headless Chrome profile; never attaches to the user's browser.
import {spawn} from 'node:child_process';
import {mkdtemp,writeFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import assert from 'node:assert/strict';

const profile=await mkdtemp(join(tmpdir(),'usage-insight-chrome-'));
const chrome=spawn('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',[
  '--headless=new','--disable-gpu','--no-first-run','--no-default-browser-check',
  '--disable-background-networking','--disable-component-update','--disable-sync',
  '--remote-debugging-port=0','--user-data-dir='+profile,'about:blank'
],{stdio:['ignore','ignore','pipe']});
let socket;
try{
const endpoint=await new Promise((resolve,reject)=>{let text='';const timer=setTimeout(()=>reject(Error('Chrome startup timed out')),15000);chrome.stderr.on('data',data=>{text+=data;const found=text.match(/DevTools listening on (ws:\/\/[^\s]+)/);if(found){clearTimeout(timer);resolve(found[1]);}});chrome.on('exit',code=>{clearTimeout(timer);reject(Error('Chrome exited '+code));});});
socket=new WebSocket(endpoint);await new Promise((resolve,reject)=>{socket.onopen=resolve;socket.onerror=reject;});
let seq=0;const pending=new Map(),errors=[];
socket.onmessage=({data})=>{const message=JSON.parse(data);if(message.id){const task=pending.get(message.id);if(task){pending.delete(message.id);message.error?task.reject(Error(JSON.stringify(message.error))):task.resolve(message.result);}}else if(message.method==='Runtime.exceptionThrown')errors.push(message.params.exceptionDetails.text);};
const send=(method,params={},sessionId)=>new Promise((resolve,reject)=>{const id=++seq;pending.set(id,{resolve,reject});socket.send(JSON.stringify({id,method,params,...(sessionId?{sessionId}:{})}));});
const {targetId}=await send('Target.createTarget',{url:'about:blank'});
const {sessionId}=await send('Target.attachToTarget',{targetId,flatten:true});
await send('Runtime.enable',{},sessionId);await send('Page.enable',{},sessionId);
await send('Emulation.setDeviceMetricsOverride',{width:1440,height:1100,deviceScaleFactor:1,mobile:false},sessionId);
const evaluate=async expression=>{const response=await send('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true},sessionId);if(response.exceptionDetails)throw Error(response.exceptionDetails.text);return response.result.value;};
const wait=async expression=>evaluate(`new Promise((resolve,reject)=>{let n=0;const timer=setInterval(()=>{if(${expression}){clearInterval(timer);resolve(true);}else if(++n>150){clearInterval(timer);reject(Error('UI condition timed out'));}},40);})`);
await send('Page.navigate',{url:'http://127.0.0.1:6198/'},sessionId);
await wait(`document.getElementById('status')?.textContent==='' && document.getElementById('people')?.children.length>0`);
assert.match(await evaluate(`document.getElementById('cost').textContent`),/CNY/);
assert.equal(await evaluate(`document.querySelectorAll('#recent tr').length`),50);
await evaluate(`document.getElementById('price-open').click()`);await wait(`document.getElementById('price-dialog').open`);
await evaluate(`for(const [key,value] of Object.entries({input:'2',cached:'.2',output:'3'})){document.querySelector('[data-field="'+key+'"]').value=value;}document.getElementById('price-save').click()`);
await wait(`document.getElementById('price-status').textContent.includes('已保存') && document.getElementById('cost').textContent.includes('CNY')`);
await evaluate(`document.getElementById('price-close').click();document.getElementById('attribution').value='participants';document.getElementById('attribution').dispatchEvent(new Event('change'))`);
await wait(`document.getElementById('status').textContent==='' && document.getElementById('people').textContent.includes('测试同伴')`);
await evaluate(`document.querySelector('#people button').click()`);await wait(`document.getElementById('status').textContent==='' && document.getElementById('person-filter').textContent.includes('当前归属')`);
await evaluate(`document.getElementById('reset').click()`);await wait(`document.getElementById('status').textContent==='' && document.querySelectorAll('#recent tr').length===50`);
await evaluate(`document.getElementById('next').click()`);await wait(`document.getElementById('status').textContent==='' && document.getElementById('page').textContent.includes('第 2 页')`);
await evaluate(`document.getElementById('prev').click()`);await wait(`document.getElementById('status').textContent==='' && document.getElementById('page').textContent.includes('第 1 页')`);
await evaluate(`document.getElementById('kind').value='memory';document.getElementById('kind').dispatchEvent(new Event('change'))`);await wait(`document.getElementById('status').textContent==='' && document.querySelectorAll('#people tr').length===1`);
await evaluate(`document.querySelector('#people button').click()`);await wait(`document.getElementById('status').textContent==='' && !document.getElementById('content').hidden`);
await evaluate(`document.getElementById('reset').click()`);await wait(`document.getElementById('status').textContent===''`);
let screenshot=await send('Page.captureScreenshot',{format:'png',captureBeyondViewport:true},sessionId);await writeFile('/private/tmp/usage-insight-desktop.png',Buffer.from(screenshot.data,'base64'));
await send('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true},sessionId);
await evaluate(`window.scrollTo(0,0)`);screenshot=await send('Page.captureScreenshot',{format:'png'},sessionId);await writeFile('/private/tmp/usage-insight-mobile.png',Buffer.from(screenshot.data,'base64'));
assert.equal(await evaluate(`document.documentElement.scrollWidth<=window.innerWidth`),true);
assert.deepEqual(errors,[]);
console.log(JSON.stringify({loaded:true,automaticOfficialPrices:true,saveOverrides:true,attribution:true,personFilter:true,sharedFilter:true,pagination:true,mobileNoOverflow:true,scriptErrors:errors}));
await send('Browser.close');
}finally{
socket?.close();if(chrome.exitCode===null&&chrome.signalCode===null){chrome.kill('SIGTERM');await new Promise(resolve=>chrome.once('exit',resolve));}await rm(profile,{recursive:true,force:true});
}
