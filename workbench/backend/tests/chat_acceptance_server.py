"""Manual browser acceptance host: real vendor DSH + deterministic local model.

Run: python -m workbench.backend.tests.chat_acceptance_server
All books, credentials and native sessions stay in .workbench/chat-acceptance.
This entry point is never imported by the production application.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import yaml

from workbench.backend import config, db
from workbench.backend.engine import dsh_chat_profile, dsh_paths
from workbench.backend.engine.dsh_chat import DshChatRuntime
from workbench.backend.services import agent_service, chapter_service, chat_run_service, project_service
from workbench.backend.tests.test_gates import GOOD_PROSE


MODEL = r'''
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
const req=createRequire(VENDOR_MANIFEST);
const {LlmAdapter,CallId}=await import(pathToFileURL(req.resolve('@deepseek-ai/dsh-llm')).href);
export const name='workbench-browser-acceptance-model';
export const inject=['llm'];
export function apply(ctx) {
  let counter=0;
  class Mock extends LlmAdapter {
    async resolveModel(provider,model) {return {provider,id:model,name:model,context:{contextWindow:100000},defaultMaxTokens:16000};}
    async *stream(options) {
      const textOf=m=>m.content.filter(b=>b.type==='text').map(b=>b.text).join('');
      const humans=options.messages.filter(m=>m.source.kind==='user');
      const message=humans.at(-1), text=textOf(message);
      const after=options.messages.slice(options.messages.lastIndexOf(message)+1);
      const step=after.filter(m=>m.source.kind==='tool').length;
      const read=path=>['read_file',{rel_path:path}];
      let actions=[];
      if(text.includes('选项验收')) actions=[['ask_user_question',{questions:[
        {id:'hero',question:'主角姓名最终确定为？',options:[{label:'林玄'},{label:'李长歌'}]},
        {id:'direction',question:'本书保留哪些方向？',multi_select:true,options:[{label:'修仙'},{label:'搜打撤'},{label:'现实侧猎妖'}]}
      ]}]];
      else if(text.includes('新建验收备忘录')) actions=[['create_file',{rel_path:'备忘录/验收线索.md',content:'茶杯缺角。',title:'验收线索'}]];
      else if(text.includes('慢速章节修改')) actions=[read('章节/第0001章.txt'),['write_file',{rel_path:'章节/第0001章.txt',content:CLEAN_BODY+'\n他把旧账本放在桌上。'}]];
      else if(text.includes('修改验收备忘录')||text.includes('慢速修改')) actions=[read('备忘录/验收线索.md'),['replace_text',{rel_path:'备忘录/验收线索.md',old_text:'茶杯缺角。',new_text:'铜钱缺口。'}]];
      else if(text.includes('多文件联动')) actions=[read('设定/人物设定.md'),['write_file',{rel_path:'设定/人物设定.md',content:'林川是码头掌柜。'}],read('大纲/大纲.md'),['write_file',{rel_path:'大纲/大纲.md',content:'第一章，林川在码头找到铜钱。'}]];
      else if(text.includes('改名验收')) actions=[read('备忘录/验收线索.md'),['move_file',{rel_path:'备忘录/验收线索.md',destination:'备忘录/验收改名.md'}]];
      else if(text.includes('删除验收')) actions=[read('备忘录/验收改名.md'),['delete_file',{rel_path:'备忘录/验收改名.md'}]];
      else if(text.includes('写下一章')) actions=[['new_chapter',{title:'码头账本',content:CLEAN_BODY}]];
      else if(text.includes('撤回')) actions=[['revert_last_changes',{}]];
      if(text.includes('失败示例')) throw new Error('本地验收模拟模型连接失败');
      if(text.includes('慢速') && step===0) {
        yield {type:'block-start',index:0,blockType:'text'};
        let body='';
        for(let i=0;i<36;i++) {
          if(options.signal.aborted) throw new Error('cancelled');
          body+='正在核对文件。';
          yield {type:'text-delta',index:0,text:'正在核对文件。'};
          await new Promise(resolve=>setTimeout(resolve,160));
        }
        yield {type:'block-end',index:0,block:{type:'text',text:body}};
      }
      if(step<actions.length) {
        const [name,args]=actions[step], id=CallId('acceptance-'+(++counter));
        const index=text.includes('慢速')&&step===0?1:0;
        yield {type:'block-start',index,blockType:'tool-call'};
        yield {type:'tool-call-delta',index,id,name,argumentsDelta:JSON.stringify(args)};
        yield {type:'block-end',index,block:{type:'tool-call',id,name,arguments:JSON.stringify(args)}};
        yield {type:'finish',reason:{kind:'tool-calls'}};
        return;
      }
      let answer=actions.length?'已按要求处理，请查看下方文件变更记录。':'这是本地验收模型的回复。';
      if(text.includes('审稿')) answer='审稿完成，本轮只读，没有修改文件。';
      if(text.includes('记忆')) answer=humans.map(textOf).find(t=>t.includes('记住'))||'没有此前记录。';
      const index=text.includes('慢速')&&step===0?1:0;
      yield {type:'block-start',index,blockType:'text'};
      for(const word of answer.match(/.{1,5}/gu)) {yield {type:'text-delta',index,text:word}; await new Promise(r=>setTimeout(r,60));}
      yield {type:'block-end',index,block:{type:'text',text:answer}};
      yield {type:'usage',usage:{inputTokens:25,outputTokens:20}};
      yield {type:'finish',reason:{kind:'stop'}};
    }
  }
  ctx.llm.registerAdapter(['workbench-acceptance'],new Mock());
}
'''


def main():
    from workbench.backend.app import check_port_available, create_app
    import uvicorn
    check_port_available(config.HOST,8790)
    lab=config.runtime_dir()/"chat-acceptance"
    lab.mkdir(parents=True,exist_ok=True)
    vendor_manifest=(dsh_paths.get_vendor_dsh_dir()/"package.json").as_uri()
    config.PROJECT_ROOT=lab
    config.RUNTIME_DIR=lab/".workbench"
    config.PROJECTS_DIR=lab/"books"
    config.DB_PATH=config.RUNTIME_DIR/"workbench.db"
    dsh_paths.DSH_HOME_DIR=config.RUNTIME_DIR/"dsh-home"
    dsh_chat_profile.get_project_root=lambda:lab
    config.ensure_runtime_dirs()
    db.init_db()
    agent_service.ensure_builtin_agents()
    books=project_service.list_projects()
    project=books[0] if books else project_service.create_project(name="对话改造验收小说")
    if not chapter_service.list_chapters(project["id"]):
        chapter=chapter_service.create_chapter(project["id"],title="码头初遇")
        chapter_service.save_chapter(project["id"],chapter["rel_path"],GOOD_PROSE*12)
    plugin=lab/"model.mjs"
    plugin.write_text(MODEL.replace('VENDOR_MANIFEST',json.dumps(vendor_manifest))
                     .replace('CLEAN_BODY',json.dumps(GOOD_PROSE*12,ensure_ascii=False)),encoding="utf-8")
    overlay=lab/"mock-model.yml"
    overlay.write_text(yaml.safe_dump([
        {"id":"llm-pi-ai","disabled":True},{"id":"llm-deepseek","disabled":True},
        {"insert":[{"id":"acceptance-model","name":plugin.as_uri()}]},
    ]),encoding="utf-8")
    with db.get_conn() as conn:
        if not conn.execute("SELECT 1 FROM providers WHERE provider_id='workbench-acceptance'").fetchone():
            conn.execute("INSERT INTO providers(provider_id,display_name,base_url,model_id,models,enabled)"
                         " VALUES ('workbench-acceptance','本地验收模型','http://127.0.0.1','mock',?,1)",
                         (json.dumps([{"id":"mock","name":"本地验收模型","context_window":100000,"max_tokens":16000}]),))
    native=DshChatRuntime(timeout_seconds=45)
    command=native._command
    native._command=lambda:[*command(),'--patch',str(overlay)]
    chat_run_service._runtime=native
    # A deterministic, delayed lightweight title response exercises the real
    # background title lifecycle without a remote provider or credentials.
    from workbench.backend.engine.direct_api import DirectApiEngine
    from workbench.backend.engine.runtime import GenerationResult
    def local_title(self, request, session_id, should_cancel, attempts, started):
        time.sleep(3)
        return GenerationResult(ok=True, text="修仙猎妖创作讨论", engine="direct-api",
                                provider="workbench-acceptance", model="mock",
                                prompt_tokens=30, completion_tokens=8, duration_ms=3000)
    DirectApiEngine._attempt=local_title
    DirectApiEngine.available=lambda self: True
    print(f"Acceptance project: http://127.0.0.1:8790/project/{project['id']}/editor",flush=True)
    uvicorn.run(create_app(),host=config.HOST,port=8790,log_level="warning")


if __name__=="__main__":
    main()
