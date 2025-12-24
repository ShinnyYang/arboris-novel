from __future__ import annotations

import json
import logging
import re
from typing import Dict, List, Optional, Tuple

from fastapi import HTTPException, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Chapter
from ..schemas.novel import Blueprint
from ..services.llm_service import LLMService
from ..services.novel_service import NovelService
from ..services.prompt_service import PromptService
from ..utils.json_utils import remove_think_tags, sanitize_json_like_text, unwrap_markdown_json

logger = logging.getLogger(__name__)


class ImportService:
    """处理小说文件导入、分章与AI分析的服务。"""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.novel_service = NovelService(session)
        self.llm_service = LLMService(session)
        self.prompt_service = PromptService(session)

    async def import_novel_from_file(self, user_id: int, file: UploadFile) -> str:
        """
        导入小说文件，执行分章、分析并创建项目。
        返回新创建的项目ID。
        """
        content = await self._read_file_content(file)
        if not content:
            raise HTTPException(status_code=400, detail="文件内容为空")

        # 1. 智能分段（分章）
        chapters = self._split_into_chapters(content)
        if not chapters:
            # 如果无法分章，将整个文件作为一个章节
            chapters = [("第一章 全文", content)]

        # 2. 准备分析用的文本样本
        # 取前3章的内容作为样本（限制总长度，避免超出token限制）
        sample_text = ""
        chapter_titles = [title for title, _ in chapters]
        
        for title, body in chapters[:3]:
            sample_text += f"{title}\n{body[:2000]}\n\n" # 每章取前2000字
        
        # 3. AI 分析
        blueprint_data = await self._analyze_content(user_id, sample_text, chapter_titles)
        
        # 4. 创建项目
        title = blueprint_data.title or file.filename.rsplit('.', 1)[0]
        initial_prompt = f"导入自文件: {file.filename}"
        project = await self.novel_service.create_project(user_id, title, initial_prompt)
        
        # 5. 保存蓝图
        # 确保 blueprint_data 中的 chapter_outline 包含所有章节（如果AI没返回全部）
        if blueprint_data.chapter_outline:
            # 建立映射以合并AI生成的摘要和实际章节列表
            ai_outlines = {o.chapter_number: o for o in blueprint_data.chapter_outline}
            final_outlines = []
            for i, (chap_title, _) in enumerate(chapters, 1):
                if i in ai_outlines:
                    outline = ai_outlines[i]
                    outline.title = chap_title # 优先使用解析出的真实标题
                else:
                    # AI未生成的章节，使用默认占位
                    from ..schemas.novel import ChapterOutline as ChapterOutlineSchema
                    outline = ChapterOutlineSchema(
                        chapter_number=i,
                        title=chap_title,
                        summary=""
                    )
                final_outlines.append(outline)
            blueprint_data.chapter_outline = final_outlines
        
        await self.novel_service.replace_blueprint(project.id, blueprint_data)
        
        # 6. 保存章节内容
        for i, (chap_title, chap_content) in enumerate(chapters, 1):
            chapter = await self.novel_service.get_or_create_chapter(project.id, i)
            # 创建初始版本
            await self.novel_service.replace_chapter_versions(
                chapter, 
                [chap_content], 
                metadata=[{"source": "file_import"}]
            )
            # 自动选择第一个版本（即导入的内容）
            await self.novel_service.select_chapter_version(chapter, 0)

        # 更新项目状态
        project.status = "blueprint_ready"
        await self.session.commit()
        
        return project.id

    async def _read_file_content(self, file: UploadFile) -> str:
        content_bytes = await file.read()
        try:
            return content_bytes.decode('utf-8')
        except UnicodeDecodeError:
            try:
                return content_bytes.decode('gbk')
            except UnicodeDecodeError:
                raise HTTPException(status_code=400, detail="文件编码不支持，请使用 UTF-8 或 GBK")

    def _split_into_chapters(self, content: str) -> List[Tuple[str, str]]:
        """
        使用正则匹配章节标题。
        支持格式：
        第1章
        第一章
        Chapter 1
        """
        # 正则表达式匹配行首的章节标题
        # 常见格式：第xxx章、Chapter xxx、xxx、(xxx)
        pattern = r"(^\s*第[0-9零一二三四五六七八九十百千]+[章卷回节].*|^\s*Chapter\s+[0-9]+.*)"
        
        # 使用 split 保留分隔符（即标题）
        parts = re.split(pattern, content, flags=re.MULTILINE)
        
        chapters = []
        # split 后第一个元素通常是标题前的内容（序章或前言），如果非空也算一章
        if parts[0].strip():
             chapters.append(("序章", parts[0].strip()))
             
        # 后续元素是 标题, 内容, 标题, 内容...
        for i in range(1, len(parts), 2):
            title = parts[i].strip()
            body = parts[i+1].strip() if i+1 < len(parts) else ""
            if body:
                chapters.append((title, body))
                
        return chapters

    async def _analyze_content(self, user_id: int, sample_text: str, chapter_titles: List[str]) -> Blueprint:
        prompt_template = await self.prompt_service.get_prompt("import_analysis")
        if not prompt_template:
            # Fallback prompt if file not found in DB
            prompt_template = """
            你是一个专业的网文编辑。请根据提供的小说样本和目录，分析并提取小说信息。
            返回 JSON 格式，包含：title, one_sentence_summary, full_synopsis, world_setting (core_rules, key_locations, factions, magic_system), characters, relationships, chapter_outline。
            注意 world_setting 的 key 必须是 core_rules 和 key_locations。
            """
            
        system_prompt = f"{prompt_template}\n\n章节列表参考：\n" + "\n".join(chapter_titles[:50]) # 仅提供前50章标题作为参考，避免过长
        
        # 构造 prompt
        messages = [{"role": "user", "content": f"请分析以下小说内容：\n\n{sample_text}"}]
        
        try:
            response = await self.llm_service.get_llm_response(
                system_prompt=system_prompt,
                conversation_history=messages,
                temperature=0.3,
                user_id=user_id,
                timeout=120.0
            )
            
            response = remove_think_tags(response)
            normalized = unwrap_markdown_json(response)
            sanitized = sanitize_json_like_text(normalized)
            data = json.loads(sanitized)
            
            # --- 数据标准化处理 (Robustness Fixes) ---
            if "world_setting" in data:
                ws = data["world_setting"]
                # 1. 兼容旧的 key 名称
                if "rules" in ws and "core_rules" not in ws:
                    ws["core_rules"] = ws.pop("rules")
                if "locations" in ws and "key_locations" not in ws:
                    ws["key_locations"] = ws.pop("locations")
                
                # 2. 确保 core_rules 是字符串 (Frontend expects string)
                # 如果 AI 返回了 list，将其合并为字符串
                if "core_rules" in ws and isinstance(ws["core_rules"], list):
                    ws["core_rules"] = "\n".join(str(r) for r in ws["core_rules"])
                elif "core_rules" not in ws:
                     ws["core_rules"] = ""
                     
                # 3. 确保 key_locations 存在
                if "key_locations" not in ws:
                    ws["key_locations"] = []

            return Blueprint(**data)
            
        except Exception as e:
            logger.error(f"AI 分析失败: {e}", exc_info=True)
            # 分析失败时返回一个空的 Blueprint，但保留标题等基本信息
            return Blueprint(
                title="导入的项目",
                one_sentence_summary="AI分析失败，请手动补充",
                chapter_outline=[]
            )
