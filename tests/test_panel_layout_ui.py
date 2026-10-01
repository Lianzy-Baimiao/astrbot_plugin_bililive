"""Offline theme, routing and pixel-alignment checks. Requires Playwright + Chromium.
Set WOW_PANEL_BROWSER to a local Chromium executable if needed.
"""
from pathlib import Path
import os, mimetypes, unittest
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, expect
ROOT = next((Path(__file__).resolve().parents[1] / "pages").iterdir())
BRIDGE = """
window.AstrBotPluginPage = {
 ready: () => Promise.resolve(),
 apiGet: async path => ({status:'ok',data:{
  session_ok:true,monitor_running:true,monitors:12,max_monitors:50,
  check_interval:60,dynamic_check_interval:180,groups:[],rows:[],records:[],shots:[],
  live:{rows:[]},dynamic:{rows:[]},config:{shot_enabled:true,shot_quality:85,shot_max_height:8000},
  match_types:[{value:'exact',label:'精确'}],formats:[{value:'text',label:'文本'}],
  rules:[],scopes:[{value:'global',label:'全局'}],stats:{total:12,enabled:10,hits:12345},enabled:true
 }}),
 apiPost: async (path,body) => {window.lastPost={path,body};return {status:'ok',data:{}};}
};
"""
class PanelLayoutTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.pw=sync_playwright().start()
  cls.browser=cls.pw.chromium.launch(headless=True,**({'executable_path':os.environ['WOW_PANEL_BROWSER']} if os.environ.get('WOW_PANEL_BROWSER') else {}))
 @classmethod
 def tearDownClass(cls):
  cls.browser.close();cls.pw.stop()
 def setUp(self):
  self.context=self.browser.new_context(viewport={'width':1600,'height':1100})
  def serve(route):
   path=urlparse(route.request.url).path
   if path.endswith('bridge-sdk.js'):route.fulfill(body=BRIDGE,content_type='text/javascript; charset=utf-8');return
   file=ROOT/path.lstrip('/')
   if file.is_file() and file.parent==ROOT:route.fulfill(body=file.read_bytes(),content_type=(mimetypes.guess_type(file.name)[0] or 'text/plain')+'; charset=utf-8')
   else:route.fulfill(status=404)
  self.context.route('http://panel.test/**',serve)
  self.page=self.context.new_page();self.errors=[]
  self.page.on('pageerror',lambda e:self.errors.append(str(e)))
  self.page.goto('http://panel.test/index.html')
  expect(self.page.locator('.connection')).to_have_text('面板已连接')
 def tearDown(self):
  self.context.close();self.assertEqual(self.errors,[])
 def test_theme_and_storage(self):
  p=self.page
  expect(p.locator('html')).to_have_attribute('data-theme','light')
  p.locator('#panelTheme').click()
  expect(p.locator('html')).to_have_attribute('data-theme','dark')
  p.reload();expect(p.locator('html')).to_have_attribute('data-theme','dark')
  p.evaluate("() => {Storage.prototype.setItem=()=>{throw Error('blocked')};}")
  p.locator('#panelTheme').click();expect(p.locator('html')).to_have_attribute('data-theme','light')
 def test_dialog_and_save_wire_contract(self):
  p=self.page
  if ROOT.name=='bililive-panel':
   p.locator('.nav-item').nth(1).click();p.locator('#btnAddSub').click()
   expect(p.locator('#subMask')).to_be_visible()
   p.locator('#subCancel').click();expect(p.locator('#subMask')).to_be_hidden()
   p.locator('#btnSaveSubs').click()
   p.wait_for_function("window.lastPost && window.lastPost.path==='page/subscriptions/save'")
   self.assertEqual(p.evaluate('window.lastPost.body'),{'kind':'live','rows':[]})
  elif ROOT.name=='wuxianews-panel':
   p.locator('#btnPickGroup').click();expect(p.locator('#pickMask')).to_be_visible()
   p.locator('#pickClose').click();expect(p.locator('#pickMask')).to_be_hidden()
   p.locator('.nav-item').nth(3).click()
   p.locator('#cfgShotQuality').fill('80');p.locator('#btnSaveConfig').click()
   p.wait_for_function("window.lastPost && window.lastPost.path==='page/config/save'")
   self.assertEqual(p.evaluate('window.lastPost.body.shot_quality'),'80')
  else:
   p.locator('#btnNew').click();expect(p.locator('#editMask')).to_be_visible()
   p.locator('#fKeyword').fill('hello');p.locator('#fReply').fill('world')
   p.locator('#editSave').click();expect(p.locator('#editMask')).to_be_hidden()
   self.assertEqual(p.evaluate('window.lastPost.path'),'page/rules')
   self.assertEqual(p.evaluate('window.lastPost.body.keyword'),'hello')
   self.assertEqual(p.evaluate('window.lastPost.body.format'),'text')

 def test_routes_and_alignment(self):
  p=self.page
  for width in [1600,1440,768,390]:
   p.set_viewport_size({'width':width,'height':1100})
   for theme in ['light','dark']:
    if p.locator('html').get_attribute('data-theme')!=theme:p.locator('#panelTheme').click()
    for i in range(p.locator('.nav-item').count()):
     p.locator('.nav-item').nth(i).click()
     self.assertEqual(p.locator('.panel-view:visible').count(),1)
     self.assertTrue(p.evaluate('document.documentElement.scrollWidth <= innerWidth+1'),(width,theme,i))
     if os.environ.get('WOW_PANEL_SCREENSHOTS') and width in [1600,390]:
      out=Path(os.environ['WOW_PANEL_SCREENSHOTS']);out.mkdir(parents=True,exist_ok=True)
      p.screenshot(path=str(out/(ROOT.name+'-view'+str(i)+'-'+str(width)+'-'+theme+'.png')),full_page=True)

    p.locator('.nav-item').first.click()
    boxes=p.locator('.stat').evaluate_all("nodes=>nodes.map(n=>({top:n.getBoundingClientRect().top,label:n.querySelector('.l').getBoundingClientRect().top,value:n.querySelector('.n').getBoundingClientRect().top,before:getComputedStyle(n.querySelector('.l'),'::before').content}))")
    for a in boxes:
     self.assertEqual(a['before'],'none')
     for b in boxes:
      if abs(a['top']-b['top'])<1:
       self.assertAlmostEqual(a['label'],b['label'],delta=1)
       self.assertAlmostEqual(a['value'],b['value'],delta=1)
    if os.environ.get('WOW_PANEL_SCREENSHOTS') and width in [1600,390]:
     out=Path(os.environ['WOW_PANEL_SCREENSHOTS']);out.mkdir(parents=True,exist_ok=True)
     p.screenshot(path=str(out/(ROOT.name+'-'+str(width)+'-'+theme+'.png')),full_page=True)
if __name__=='__main__':unittest.main()
