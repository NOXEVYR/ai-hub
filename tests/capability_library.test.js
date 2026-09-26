const test=require('node:test'),assert=require('node:assert/strict');
const ui=require('../frontend/capability-library.js');
test('purpose and keyword jointly filter discovered resources',()=>{
 const items=[{name:'视频创作',domains:['video'],tool:'codex'},{name:'图像',domains:['image'],tool:'workbuddy'}];
 assert.deepEqual(ui.filter(items,'codex','video'),[items[0]]);assert.equal(ui.filter(items,'codex','image').length,0);
});
test('discovered skills expose escaped paths and next steps without an execute button',()=>{
 const html=ui.inventoryHTML([{name:'<bad>',path:'C:/skills/"bad',description:'<script>',domains:['image']}],'skills');
 assert(!html.includes('<script>'));assert.match(html,/data-skill-path="C:\/skills\/&quot;bad/);assert.match(html,/在所属工作端启用/);assert(!html.includes('data-cp-select'));
});
test('credential renderer never serializes values or secrets',()=>{
 const html=ui.credentialsHTML([{name:'TOKEN_NAME',present:true,value:'top-secret',token:'private'}]);
 assert.match(html,/TOKEN_NAME/);assert.match(html,/已配置/);assert(!html.includes('top-secret'));assert(!html.includes('private'));
});
test('fresh workspace guide explains ownership and offers an explicit setup path',()=>{
 const ws=require('../frontend/workspace.js');assert.match(ws.welcome(false),/开始设置工作环境/);assert.match(ws.welcome(false),/图像生成、视频创作和代码执行仍由各自的工具完成/);assert.match(ws.welcome(true),/^<details/);
});
test('all resource kinds use independent purpose and work-end dimensions',()=>{
 const items=[{kind:'skill',name:'多用途',domains:['video','code'],tools:['codex','dsh']},{kind:'interface',name:'独立视频接口',domains:['video'],tools:[]},{kind:'credential',variable:'VIDEO_TOKEN',domains:['video'],tools:['dsh']}];
 assert.deepEqual(ui.filter(items,'','video','dsh'),[items[0],items[2]]);
 assert.deepEqual(ui.filter(items,'','video','__independent'),[items[1]]);
 assert.deepEqual(ui.filter(items,'VIDEO_TOKEN','video','dsh'),[items[2]]);
 assert.deepEqual(ui.filter(items,'','code','codex'),[items[0]]);
});

test('plugin interface metadata has a searchable visible name',()=>{
 const item={plugin_name:'example-mcp',description:'Service declaration',domains:[]};
 assert.match(ui.inventoryHTML([item],'interfaces'),/<h3>example-mcp<\/h3>/);
 assert.deepEqual(ui.filter([item],'example-mcp',''),[item]);
});
