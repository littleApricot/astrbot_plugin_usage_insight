// Actual API handlers over pipes plus a new, disposable headless browser profile.
import {createRequire} from 'node:module';
import {spawn} from 'node:child_process';
import {readFile,mkdtemp,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {dirname,resolve,join,delimiter} from 'node:path';
import {fileURLToPath} from 'node:url';
import {createInterface} from 'node:readline';
import assert from 'node:assert/strict';

const require=createRequire(import.meta.url);
const {chromium}=require(process.env.PLAYWRIGHT_PACKAGE||'playwright');
if(!process.env.ASTRBOT_SOURCE)throw Error('Set ASTRBOT_SOURCE to an AstrBot source checkout');
const runtime=await mkdtemp(join(tmpdir(),'usage-insight-offline-'));
const parent=resolve(dirname(fileURLToPath(import.meta.url)),'..','..');
const python=spawn(process.env.ASTRBOT_PYTHON||'python3',['tests/ui_rpc.py'],{
 env:{...process.env,PYTHONDONTWRITEBYTECODE:'1',PYTHONPATH:[process.env.ASTRBOT_SOURCE,parent].join(delimiter),ASTRBOT_ROOT:runtime,ASTRBOT_DISABLE_METRICS:'1'},stdio:['pipe','pipe','pipe']
});
let sequence=0;const pending=new Map();
const lines=createInterface({input:python.stdout});
lines.on('line',line=>{if(!line.startsWith('USAGE_RPC:'))return;const message=JSON.parse(line.slice(10));const task=pending.get(message.id);if(task){clearTimeout(task.timer);pending.delete(message.id);task.resolve(message.result);}});
function api(method,path,body){return new Promise((resolve,reject)=>{const id=++sequence;const timer=setTimeout(()=>{pending.delete(id);reject(Error('API pipe timed out'));},15000);pending.set(id,{resolve,reject,timer});python.stdin.write(JSON.stringify({id,method,path,body})+'\n');});}
let browser;
try{
 const initial=await api('GET','report?days=0');assert.equal(initial.count,125);
 browser=await chromium.launch({headless:true,executablePath:process.env.TEST_CHROME||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',args:['--disable-background-networking']});
 const page=await browser.newPage({viewport:{width:1440,height:1100}}),errors=[];
 page.on('pageerror',error=>errors.push(error.message));
 await page.route('**/*',route=>route.abort());
 await page.exposeFunction('usageFixtureAPI',api);
 const bridge=`<script>window.AstrBotPluginPage={apiGet:(path,params)=>window.usageFixtureAPI('GET',path+'?'+new URLSearchParams(params||{})),apiPost:(path,body)=>window.usageFixtureAPI('POST',path,body)};</script>`;
 const html=await readFile('pages/usage/index.html','utf8');
 await page.setContent(html.replace('<head>','<head>'+bridge));
 await page.waitForFunction(()=>document.getElementById('status').textContent===''&&document.querySelectorAll('#people tr').length>0);
 await page.waitForFunction(()=>document.getElementById('balance-status').textContent===''&&document.getElementById('account-balances').textContent.includes('14.95'));
 await page.locator('#balance-refresh').click();await page.waitForFunction(()=>document.getElementById('balance-status').textContent===''&&document.getElementById('account-balances').textContent.includes('14.94'));
 assert.match(await page.locator('#key-usage').innerText(),/旧记录缺少 Key 标记/);
 assert.equal(await page.locator('#key-usage tr').count(),2);
 assert.equal(await page.locator('#key-usage tr').first().locator('td').nth(2).innerText(),'1');
 assert.match(await page.locator('#key-usage tr').first().innerText(),/本 bot（当前 Key）/);
 assert.match(await page.locator('#key-usage tr').nth(1).innerText(),/其他应用（同账号）/);
 assert.equal(await page.locator('#key-usage tr').nth(1).locator('td').filter({hasText:/^未知$/}).count(),5);
 assert.match(await page.locator('#cost').innerText(),/CNY/);
 assert.match(await page.locator('#cost-coverage').innerText(),/未匹配价格/);
 assert.match(await page.locator('#people').innerText(),/宿主历史（未记录用户）/);
 assert.equal(await page.locator('#recent tr').count(),50);
 await page.locator('#price-open').click();await page.locator('#price-dialog').waitFor({state:'visible'});
 assert.equal(await page.locator('#auto-prices tr').count(),4);
 assert.match(await page.locator('#auto-price-status').innerText(),/已识别 1 个/);
 assert.equal(await page.locator('#price-rows tr').count(),1);
 await page.screenshot({path:'/private/tmp/usage-insight-prices.png'});
 for(const [key,value] of Object.entries({input:'2',cached:'.2',output:'3'}))await page.locator(`[data-field="${key}"]`).fill(value);
 await page.locator('#price-save').click();await page.waitForFunction(()=>document.getElementById('price-status').textContent.includes('已保存')&&document.getElementById('cost').textContent.includes('CNY'));
 await page.locator('#price-close').click();
 await page.locator('#attribution').selectOption('participants');await page.waitForFunction(()=>document.getElementById('status').textContent===''&&document.getElementById('people').textContent.includes('测试同伴'));
 await page.locator('#people button').first().click();await page.waitForFunction(()=>document.getElementById('status').textContent===''&&document.getElementById('person-filter').textContent.includes('当前归属'));
 await page.locator('#reset').click();await page.waitForFunction(()=>document.getElementById('status').textContent===''&&document.querySelectorAll('#recent tr').length===50);
 await page.locator('#next').click();await page.waitForFunction(()=>document.getElementById('status').textContent===''&&document.getElementById('page').textContent.includes('第 2 页'));
 await page.locator('#prev').click();await page.waitForFunction(()=>document.getElementById('status').textContent===''&&document.getElementById('page').textContent.includes('第 1 页'));
 await page.locator('#kind').selectOption('memory');await page.waitForFunction(()=>document.getElementById('status').textContent===''&&document.querySelectorAll('#people tr').length===1);
 await page.locator('#people button').click();await page.waitForFunction(()=>document.getElementById('status').textContent===''&&!document.getElementById('content').hidden);
 await page.locator('#reset').click();await page.waitForFunction(()=>document.getElementById('status').textContent==='');
 assert.equal(await page.getByRole('button',{name:'官方明细核对'}).count(),0);
 await page.setViewportSize({width:1440,height:1100});await page.screenshot({path:'/private/tmp/usage-insight-desktop.png',fullPage:true});
 await page.setViewportSize({width:390,height:844});await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));assert.equal(await page.locator('h1').count(),1);await page.screenshot({path:'/private/tmp/usage-insight-mobile.png'});await page.locator('#people').scrollIntoViewIfNeeded();await page.screenshot({path:'/private/tmp/usage-insight-mobile-people.png'});
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true);
 assert.deepEqual(errors,[]);
 console.log(JSON.stringify({loaded:true,officialBalanceRefresh:true,isolatedKeyUsage:true,botAndOtherAppScopes:true,automaticOfficialPrices:true,priceReasons:true,separateHistoricalAttribution:true,saveOverrides:true,attribution:true,personFilter:true,sharedFilter:true,pagination:true,mobileNoOverflow:true,scriptErrors:errors,apiTransport:'in-process ASGI over pipes'}));
}finally{
 if(browser)await browser.close();
 lines.close();python.stdin.end();if(python.exitCode===null&&python.signalCode===null){const exited=new Promise(resolve=>python.once('exit',resolve));python.kill('SIGTERM');await exited;}await rm(runtime,{recursive:true,force:true});
 for(const task of pending.values()){clearTimeout(task.timer);task.reject(Error('fixture closed'));}
}
