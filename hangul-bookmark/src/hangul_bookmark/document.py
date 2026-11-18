"""Structured, revision-bound document model. Document text is untrusted data."""
import copy
import hashlib
import json
import re
from .errors import WriterError
from . import xmlcheck
from .latex import convert, validate_script

EXCLUDED = {'HEADER','FOOTER','FOOTNOTE','ENDNOTE','TEXTBOX','DRAWTEXT','MASTERPAGE'}
LANGS = ('Hangul','Latin','Hanja','Japanese','Other','Symbol','User')

def fail(code, message):
    raise WriterError(code, message)

def encode(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(',',':'))

def digest(obj):
    return hashlib.sha256(encode(obj).encode()).hexdigest()

def revision(xml):
    root = xmlcheck.parse(xml)
    for parent in root.iter():
        # Hancom 2022 can vary the exported encoded-length hint without changing
        # embedded bytes. Keep the full data in the hash; ignore only this hint.
        if xmlcheck.tag(parent)=='BINDATA':parent.attrib.pop('Size',None)
        for child in list(parent):
            if xmlcheck.tag(child) in ('DOCSUMMARY','DOCSETTING'): parent.remove(child)
    return digest(xmlcheck.ET.tostring(root,encoding='unicode'))

def uid(rev, path):
    return hashlib.sha256((rev+'|'+path).encode()).hexdigest()[:32]

def units(text):
    return len(text.encode('utf-16-le'))//2

def canonical(node, ignore=()):
    return [xmlcheck.tag(node),{k:v for k,v in node.attrib.items() if k not in ignore},
            node.text or '',[canonical(c,ignore) for c in node]]

def model(xml):
    root=xmlcheck.parse(xml); rev=revision(xml)
    fonts={f.get('Lang'):{n.get('Id'):n.get('Name') for n in f if xmlcheck.tag(n)=='FONT'} for f in root.iter() if xmlcheck.tag(f)=='FONTFACE'}
    charstyles={}
    for s in root.iter():
        if xmlcheck.tag(s)!='CHARSHAPE': continue
        others=copy.deepcopy(s);others.attrib.pop('Id',None);others.attrib.pop('Height',None)
        names={}
        for c in list(others):
            if xmlcheck.tag(c)=='FONTID':
                names={lang:fonts.get(lang,{}).get(c.get(lang),'?'+str(c.get(lang))) for lang in LANGS}
                others.remove(c)
        tag_names={xmlcheck.tag(c) for c in s}
        charstyles[s.get('Id')]={
            'size_pt':int(s.get('Height','1000'))/100,
            'fonts':names,
            'bold':('BOLD' in tag_names),
            'italic':('ITALIC' in tag_names),
            'underline':('UNDERLINE' in tag_names),
            'strikethrough':('STRIKEOUT' in tag_names),
            'text_color':s.get('TextColor'),
            'other':canonical(others),
        }
    parastyles={s.get('Id'):canonical(s,('Id',)) for s in root.iter() if xmlcheck.tag(s)=='PARASHAPE'}
    styles={s.get('Id'):s.attrib for s in root.iter() if xmlcheck.tag(s)=='STYLE'}
    paragraphs=[];tables=[];targets={};excluded={}
    def walk(node,path,context,unsupported=False):
        tag=xmlcheck.tag(node);unsupported=unsupported or tag in EXCLUDED
        if tag in EXCLUDED: excluded[tag]=excluded.get(tag,0)+1
        if tag=='TABLE':
            number=0 if unsupported else len(tables)+1
            table={'number':number,'xml_target_id':uid(rev,path),'path':context+ [{'table':number}],'rows':int(node.get('RowCount','0')),'columns':int(node.get('ColCount','0'))}
            if not unsupported:tables.append(table)
            context=table['path']
        if tag=='CELL':
            context=context+[{'row':int(node.get('RowAddr','0'))+1,'column':int(node.get('ColAddr','0'))+1,
                              'row_span':int(node.get('RowSpan','1')),'column_span':int(node.get('ColSpan','1'))}]
        if tag=='P':
            p={'id':uid(rev,path),'path':path,'index':len(paragraphs),'scope':'excluded' if unsupported else ('table' if context else 'body'),
               'cell_path':copy.deepcopy(context),'parts':[],'text':'','length':0,
               'paragraph_shape':parastyles.get(node.get('ParaShape')),'style':dict(styles.get(node.get('Style'),{})),
               'attributes':{k:v for k,v in node.attrib.items() if k not in ('InstId','ParaShape','Style')}}
            paragraphs.append(p)
            position=0
            def inline(n,style,path2):
                nonlocal position
                kind=xmlcheck.tag(n)
                if kind=='TEXT': style=charstyles.get(n.get('CharShape'),{})
                if kind=='CHAR':
                    text=n.text or ''
                    if text:
                        part={'kind':'text','id':uid(rev,path2),'start':position,'end':position+units(text),'text':text,'style':copy.deepcopy(style)}
                        p['parts'].append(part);p['text']+=text;position=part['end']
                    return
                if kind=='EQUATION':
                    script=''.join(x.text or '' for x in n.iter() if xmlcheck.tag(x)=='SCRIPT')
                    part={'kind':'equation','id':uid(rev,path2),'start':position,'end':position+8,'script':script,
                          'size_pt':int(n.get('BaseUnit','1000'))/100,'font':n.get('Font','HancomEQN'),
                          'attributes':{k:v for k,v in n.attrib.items() if k not in ('BaseUnit','BaseLine','Font')}}
                    p['parts'].append(part);position+=8;return
                if kind in ('P','PARALIST'): return
                if kind not in ('TEXT',):
                    # HWP extended/inline controls occupy eight UTF-16 code units.
                    if kind in ('LINEBREAK','HYPHEN','NBSPACE','FWSPACE'):
                        value={'LINEBREAK':'\n','HYPHEN':'-','NBSPACE':'\u00a0','FWSPACE':'\u2007'}[kind]
                        p['parts'].append({'kind':'control','id':uid(rev,path2),'start':position,'end':position+1,'type':kind,'value':value})
                        position+=1;return
                    if kind=='TAB':
                        p['parts'].append({'kind':'control','id':uid(rev,path2),'start':position,'end':position+8,'type':kind,'value':'\t'})
                        position+=8;return
                    if kind not in ('TEXT',):
                        p['parts'].append({'kind':'control','id':uid(rev,path2),'start':position,'end':position+8,'type':kind,
                                          'value':None if kind=='TABLE' else canonical(n,('InstId',))})
                        if kind=='PICTURE':
                            from .images import descriptor
                            p['parts'][-1]['image']=descriptor(n,root)
                        position+=8;return
                for i,c in enumerate(n): inline(c,style,path2+'/'+str(i))
            for i,c in enumerate(node): inline(c,{},path+'/'+str(i))
            p['length']=position
            if not p['parts']:
                first=next((c for c in node if xmlcheck.tag(c)=='TEXT'),None)
                p['empty_style']=copy.deepcopy(charstyles.get(first.get('CharShape'),{})) if first is not None else {}
        for i,c in enumerate(node): walk(c,path+'/'+str(i),context,unsupported)
    body=next((n for n in root if xmlcheck.tag(n)=='BODY'),None)
    if body is None: fail('EXPORT_FAILED','본문 구조가 없습니다.')
    walk(body,'body',[])
    result={'revision':rev,'paragraphs':paragraphs,'tables':tables,'excluded':excluded,
            'table_structure':xmlcheck.snapshot(xml)['tables'],'bookmarks':xmlcheck.bookmarks(xml),'targets':targets}
    for p in paragraphs:
        if p['scope']=='excluded': continue
        targets[p['id']]={'kind':'range','paragraph':p['index'],'start':0,'end':p['length']}
        for part in p['parts']:
            targets[part['id']]={'kind':part['kind'],'paragraph':p['index'],'start':part['start'],'end':part['end']}
    targets['all']={'kind':'group','members':[p['id'] for p in paragraphs if p['scope']!='excluded']}
    return result

def public_paragraph(p):
    out={k:copy.deepcopy(v) for k,v in p.items() if k not in ('native','mapped','path')}
    out['target_id']=out.pop('id')
    for part in out['parts']:
        part['target_id']=part.pop('id')
    return out

def compile_advanced_edits(doc,edits):
    """Validate structural/page/header edits before invoking COM."""
    compiled=[]
    table_numbers={t['number'] for t in doc['tables']}
    page_keys={
        'apply_to','landscape','paper_width_mm','paper_height_mm','top_mm','bottom_mm',
        'left_mm','right_mm','header_mm','footer_mm','gutter_mm','gutter_type',
    }
    table_ops={
        'append_row','delete_row','append_column','delete_column',
        'merge_cells','split_cell','set_column_width','set_row_height',
    }
    for edit in edits:
        kind=edit.get('type')
        if edit.get('uncertain'):fail('UNCERTAIN_CONTENT','불명확한 문서 구조 변경은 먼저 확인하세요.')
        if kind=='set_page_layout':
            props=edit.get('properties')
            if not isinstance(props,dict) or not props or set(props)-page_keys:
                fail('INVALID_EDIT','지원하지 않는 페이지 설정 속성입니다.')
            apply_to=props.get('apply_to','cur')
            if apply_to not in ('cur','all','new'):fail('INVALID_EDIT','apply_to는 cur, all, new입니다.')
            for key,value in props.items():
                if key=='apply_to':continue
                if key=='landscape':
                    if not isinstance(value,bool):fail('INVALID_EDIT','landscape는 불리언입니다.')
                elif key=='gutter_type':
                    if isinstance(value,bool) or not isinstance(value,int) or value not in (0,1,2):fail('INVALID_EDIT','gutter_type은 0, 1, 2입니다.')
                else:
                    if isinstance(value,bool) or not isinstance(value,(int,float)):
                        fail('INVALID_EDIT','페이지 크기와 여백은 밀리미터 숫자입니다.')
                    if key in ('paper_width_mm','paper_height_mm') and not 25<=value<=500:
                        fail('INVALID_EDIT','용지 크기는 25~500mm입니다.')
                    if key!='paper_width_mm' and key!='paper_height_mm' and not 0<=value<=300:
                        fail('INVALID_EDIT','여백은 0~300mm입니다.')
            compiled.append({'type':kind,'properties':props})
        elif kind=='edit_table':
            table=edit.get('table')
            op=edit.get('operation')
            if isinstance(table,bool) or not isinstance(table,int) or table not in table_numbers:
                fail('TARGET_MISSING','존재하지 않는 표 번호입니다.')
            table_info=next(t for t in doc['tables'] if t['number']==table)
            if op not in table_ops:fail('INVALID_EDIT','지원하지 않는 표 구조 작업입니다.')
            one={}
            if op in ('append_row','delete_row','append_column','delete_column'):
                for key in ('row','column'):
                    if key in edit:
                        value=edit[key]
                        if isinstance(value,bool) or not isinstance(value,int) or value<1:
                            fail('INVALID_EDIT',key+'은 1부터 시작합니다.')
                        one[key]=value
                if 'row' in one and one['row']>table_info['rows']:fail('TARGET_MISSING','표에 해당 행이 없습니다.')
                if 'column' in one and one['column']>table_info['columns']:fail('TARGET_MISSING','표에 해당 열이 없습니다.')
            elif op=='merge_cells':
                if not isinstance(edit.get('row'),int) or not isinstance(edit.get('column'),int):
                    fail('INVALID_EDIT','병합 범위의 시작 행과 열이 필요합니다.')
                one.update(row=edit['row'],column=edit['column'],row_span=edit.get('row_span',1),column_span=edit.get('column_span',1))
                if any(isinstance(v,bool) or not isinstance(v,int) or v<1 for v in (one['row_span'],one['column_span'])):
                    fail('INVALID_EDIT','병합 행·열 범위는 1 이상이어야 합니다.')
                if one['row']+one['row_span']-1>table_info['rows'] or one['column']+one['column_span']-1>table_info['columns']:
                    fail('TARGET_MISSING','병합 범위가 표 밖입니다.')
            elif op=='split_cell':
                if not isinstance(edit.get('row'),int) or not isinstance(edit.get('column'),int):
                    fail('INVALID_EDIT','나눌 셀의 행과 열이 필요합니다.')
                rows=edit.get('rows',2);cols=edit.get('columns',1)
                if any(isinstance(v,bool) or not isinstance(v,int) or v<1 or v>100 for v in (rows,cols)):
                    fail('INVALID_EDIT','나눌 행·열은 1~100이어야 합니다.')
                one.update(row=edit['row'],column=edit['column'],rows=rows,columns=cols)
                if edit['row']<1 or edit['column']<1 or edit['row']>table_info['rows'] or edit['column']>table_info['columns']:
                    fail('TARGET_MISSING','나눌 셀이 표 밖입니다.')
            elif op=='set_column_width':
                if not isinstance(edit.get('column'),int) or isinstance(edit.get('width_mm'),bool) or not isinstance(edit.get('width_mm'),(int,float)):
                    fail('INVALID_EDIT','열 번호와 width_mm이 필요합니다.')
                if edit['column']<1 or edit['column']>table_info['columns'] or not 1<=edit['width_mm']<=500:
                    fail('INVALID_EDIT','열 번호 또는 너비가 표 범위를 벗어났습니다.')
                one.update(column=edit['column'],width_mm=edit['width_mm'])
            elif op=='set_row_height':
                if not isinstance(edit.get('row'),int) or isinstance(edit.get('height_mm'),bool) or not isinstance(edit.get('height_mm'),(int,float)):
                    fail('INVALID_EDIT','행 번호와 height_mm이 필요합니다.')
                if edit['row']<1 or edit['row']>table_info['rows'] or not 1<=edit['height_mm']<=500:
                    fail('INVALID_EDIT','행 번호 또는 높이가 표 범위를 벗어났습니다.')
                one.update(row=edit['row'],height_mm=edit['height_mm'])
            compiled.append({'type':kind,'table':table,'operation':op,**one})
        elif kind=='set_header_footer':
            area=edit.get('area')
            if area not in ('header','footer'):fail('INVALID_EDIT','area는 header 또는 footer입니다.')
            text=edit.get('text')
            if not isinstance(text,str) or len(text)>20000:fail('INVALID_EDIT','머리글·바닥글은 20,000자 이내 문자열입니다.')
            mode=edit.get('mode','replace')
            if mode not in ('replace','delete'):fail('INVALID_EDIT','mode는 replace 또는 delete입니다.')
            compiled.append({'type':kind,'area':area,'text':text,'mode':mode,'section':edit.get('section',0)})
        elif kind=='insert_note':
            area=edit.get('area')
            text=edit.get('text')
            if area not in ('footnote','endnote'):fail('INVALID_EDIT','area는 footnote 또는 endnote입니다.')
            if not isinstance(text,str) or not text or len(text)>20000:fail('INVALID_EDIT','각주·미주 내용은 1~20,000자여야 합니다.')
            target=edit.get('target_id')
            if target is not None:
                ranges=expand(doc,target)
                if len(ranges)!=1:fail('INVALID_EDIT','각주·미주는 하나의 위치에 삽입하세요.')
            compiled.append({'type':kind,'area':area,'text':text,'target_id':target})
        elif kind=='insert_comment':
            text=edit.get('text');mode=edit.get('mode','insert')
            if not isinstance(text,str) or not text or len(text)>20000:fail('INVALID_EDIT','숨은설명은 1~20,000자여야 합니다.')
            if mode not in ('insert','delete','modify'):fail('INVALID_EDIT','mode는 insert, delete, modify입니다.')
            compiled.append({'type':kind,'text':text,'mode':mode,'target_id':edit.get('target_id')})
        elif kind=='insert_field':
            field=edit.get('field')
            if field not in ('date_time','page_number','document_info','memo','citation','mail_merge','cross_reference'):fail('INVALID_EDIT','지원하지 않는 필드 종류입니다.')
            text=edit.get('text','')
            if not isinstance(text,str) or len(text)>20000:fail('INVALID_EDIT','필드 값은 20,000자 이내여야 합니다.')
            compiled.append({'type':kind,'field':field,'text':text,'target_id':edit.get('target_id')})
        elif kind=='insert_textbox':
            text=edit.get('text');mode=edit.get('mode','insert')
            if not isinstance(text,str) or len(text)>20000:fail('INVALID_EDIT','글상자 내용은 20,000자 이내여야 합니다.')
            if mode not in ('insert','delete','edit','link','unlink'):fail('INVALID_EDIT','지원하지 않는 글상자 작업입니다.')
            compiled.append({'type':kind,'text':text,'mode':mode})
        elif kind=='break_section':
            compiled.append({'type':kind,'section_type':edit.get('section_type','next')})
        elif kind=='set_page_number':
            start=edit.get('start')
            position=edit.get('position','bottom_center')
            number_format=edit.get('number_format','arabic')
            if start is not None and (isinstance(start,bool) or not isinstance(start,int) or not 1<=start<=9999):fail('INVALID_EDIT','쪽 번호 시작값은 1~9999입니다.')
            if position not in ('top_left','top_center','top_right','bottom_left','bottom_center','bottom_right','outside','inside'):
                fail('INVALID_EDIT','지원하지 않는 쪽 번호 위치입니다.')
            if number_format not in ('arabic','roman_upper','roman_lower','hangul','hangul_jamo'):fail('INVALID_EDIT','지원하지 않는 쪽 번호 형식입니다.')
            compiled.append({'type':kind,'start':start,'position':position,'number_format':number_format})
        elif kind in ('generate_index','generate_toc','apply_track_changes','break_section'):
            compiled.append({'type':kind})
        elif kind=='set_track_changes':
            enabled=edit.get('enabled')
            if not isinstance(enabled,bool):fail('INVALID_EDIT','enabled는 불리언입니다.')
            compiled.append({'type':kind,'enabled':enabled})
        elif kind=='set_paragraph_layout':
            allowed={'target_id','alignment','left_mm','right_mm','first_line_mm','line_spacing','protect','keep_with_next'}
            props={k:v for k,v in edit.items() if k!='type'}
            if set(props)-allowed or not props:fail('INVALID_EDIT','지원하지 않는 문단 서식 속성입니다.')
            target=props.get('target_id')
            if target is not None:
                ranges=expand(doc,target)
                if not ranges:fail('TARGET_MISSING','문단 서식 대상을 찾지 못했습니다.')
            if props.get('alignment') not in (None,'left','center','right','justify','distribute'):fail('INVALID_EDIT','지원하지 않는 문단 맞춤입니다.')
            for key in ('left_mm','right_mm','first_line_mm','line_spacing'):
                if key in props:
                    value=props[key]
                    if isinstance(value,bool) or not isinstance(value,(int,float)):fail('INVALID_EDIT','문단 수치 값은 숫자여야 합니다.')
                    if key=='line_spacing' and not 50<=value<=500:fail('INVALID_EDIT','줄 간격은 50~500%입니다.')
                    if key!='line_spacing' and not -300<=value<=300:fail('INVALID_EDIT','문단 들여쓰기는 -300~300mm입니다.')
            for key in ('protect','keep_with_next'):
                if key in props and not isinstance(props[key],bool):fail('INVALID_EDIT',key+'는 불리언입니다.')
            compiled.append({'type':kind,**props})
        elif kind=='set_table_format':
            allowed={'table','border','header_repeat','cell_padding_mm','align'}
            if not isinstance(edit.get('table'),int) or edit['table'] not in table_numbers:fail('TARGET_MISSING','표 번호가 없습니다.')
            props={k:v for k,v in edit.items() if k!='type'}
            if set(props)-allowed:fail('INVALID_EDIT','지원하지 않는 표 서식 속성입니다.')
            if props.get('border') not in (None,'all','outside','inside','none'):fail('INVALID_EDIT','지원하지 않는 표 테두리입니다.')
            if props.get('align') not in (None,'left','center','right'):fail('INVALID_EDIT','지원하지 않는 표 정렬입니다.')
            if 'header_repeat' in props and not isinstance(props['header_repeat'],bool):fail('INVALID_EDIT','header_repeat는 불리언입니다.')
            compiled.append({'type':kind,**props})
        elif kind=='set_object_layout':
            action=edit.get('action')
            if action not in ('move_left','move_right','move_up','move_down','bring_front','send_back','group','ungroup',
                              'wrap_square','wrap_top_bottom','lock','unlock','horz_flip','vert_flip','delete'):
                fail('INVALID_EDIT','지원하지 않는 개체 배치 작업입니다.')
            compiled.append({'type':kind,'action':action,'target_id':edit.get('target_id')})
    return compiled

def expand(doc,target):
    if target not in doc['targets']: fail('TARGET_MISSING','분석한 문서에 없는 대상입니다. 다시 읽으세요.')
    entry=doc['targets'][target]
    if entry['kind']=='group':
        return [r for m in entry['members'] for r in expand(doc,m)]
    return [dict(entry)]

def add_target(doc,entry,key=None):
    key=key or uid(doc['revision'],encode(entry))
    doc['targets'][key]=entry
    return key

def locate(doc,selector):
    if not isinstance(selector,dict): fail('INVALID_SELECTOR','selector 객체가 필요합니다.')
    kind=selector.get('type','text');hits=[]
    if kind=='all': return [{'target_id':'all','kind':'group'}]
    if kind=='selection':
        if not doc.get('selection_target'): fail('SELECTION_MISSING','한글에서 범위를 선택한 뒤 문서를 다시 선택하세요.')
        return [{'target_id':doc['selection_target'],'kind':'group'}]
    for p in doc['paragraphs']:
        if p['scope']=='excluded': continue
        if kind=='table':
            found=False
            for ti,cell in zip(p['cell_path'][::2],p['cell_path'][1::2]):
                if ti['table']!=selector.get('table'): continue
                row,col=selector.get('row'),selector.get('column')
                if row is not None and not cell['row']<=row<cell['row']+cell['row_span']:continue
                if col is not None and not cell['column']<=col<cell['column']+cell['column_span']:continue
                found=True
            if found: hits.append({'target_id':p['id'],'kind':'range','paragraph':p['index'],'text':p['text'],'cell_path':p['cell_path']})
        elif kind=='image':
            for part in p['parts']:
                if part.get('type')=='PICTURE':
                    hits.append({'target_id':part['id'],'kind':'image','paragraph':p['index'],'cell_path':p['cell_path'],'embedded':part['image']['embedded'],'format':part['image']['format']})
        elif kind=='paragraph':
            if p['id']==selector.get('target_id') or p['index']==selector.get('index'):hits.append({'target_id':p['id'],'kind':'range','text':p['text']})
        elif kind in ('text','equation'):
            needle=selector.get('text') if kind=='text' else selector.get('script')
            if kind=='text' and (not isinstance(needle,str) or not needle):fail('INVALID_SELECTOR','검색할 text가 필요합니다.')
            candidates=[]
            for part in p['parts']:
                if part['kind']!=kind:continue
                if kind=='text' and candidates and candidates[-1]['end']==part['start']:
                    candidates[-1]['text']+=part['text'];candidates[-1]['end']=part['end']
                else:candidates.append(copy.deepcopy(part))
            for part in candidates:
                if kind=='equation':
                    if needle is None or needle in part['script']:
                        hits.append({'target_id':part['id'],'kind':kind,'script':part['script'],'paragraph':p['index'],'cell_path':p['cell_path']})
                else:
                    for match in re.finditer(re.escape(needle),part['text']):
                        start=part['start']+units(part['text'][:match.start()]);end=start+units(needle)
                        entry={'kind':'text','paragraph':p['index'],'start':start,'end':end}
                        hits.append({'target_id':add_target(doc,entry),'kind':'text','text':needle,'paragraph':p['index'],
                                     'context':part['text'][max(0,match.start()-120):match.end()+120],'context_is_excerpt':True,'cell_path':p['cell_path']})
        elif kind=='heading':
            if p['scope']=='body' and p['text']==selector.get('text'):
                # Automatic boundary needs an actual heading style; otherwise ask.
                def level(item):
                    attrs=(item.get('paragraph_shape') or [None,{}])[1]
                    if attrs.get('HeadingType') not in (None,'None'):
                        return int(attrs.get('Level','0'))
                    name=item['style'].get('Name','')
                    if re.search(r'제목|개요|heading',name,re.I):
                        number=re.search(r'([0-9]+)',name)
                        return int(number.group(1)) if number else 0
                    return None
                heading_level=level(p)
                if heading_level is None and selector.get('end_heading') is None:
                    fail('AMBIGUOUS_BOUNDARY','제목 스타일이 명확하지 않습니다. 끝 제목이나 대상 문단을 지정하세요.')
                members=[];found_end=False
                for q in doc['paragraphs'][p['index']+1:]:
                    if q['scope']=='body':
                        qlevel=level(q)
                        if (selector.get('end_heading') is not None and q['text']==selector['end_heading']) or (
                            selector.get('end_heading') is None and qlevel is not None and qlevel<=heading_level):
                            found_end=True;break
                    if q['scope']!='excluded':members.append(q['id'])
                if selector.get('end_heading') is not None and not found_end:
                    fail('AMBIGUOUS_BOUNDARY','끝 제목을 찾지 못했습니다.')
                if not members:fail('TARGET_MISSING','제목 아래에 대상 문단이 없습니다.')
                hits.append({'target_id':add_target(doc,{'kind':'group','members':members}),'kind':'group','heading':p['text']})
        elif kind not in ('bookmark',): fail('INVALID_SELECTOR','지원하지 않는 selector type입니다.')
    if kind=='bookmark':
        key=doc.get('bookmark_targets',{}).get(selector.get('name'))
        if key: hits=[{'target_id':key,'kind':'range','name':selector.get('name')}]
    occurrence=selector.get('occurrence')
    if occurrence is not None:
        if type(occurrence)!=int or occurrence<1:fail('INVALID_SELECTOR','occurrence는 1부터 시작합니다.')
        hits=hits[occurrence-1:occurrence]
    return hits

def compile_edits(doc,edits):
    if not isinstance(edits,list) or not edits or len(edits)>1000:fail('INVALID_EDIT','1~1000개 변경이 필요합니다.')
    advanced_kinds={
        'set_page_layout','edit_table','set_header_footer','insert_note','insert_comment',
        'insert_field','insert_textbox','break_section','set_page_number','generate_index',
        'generate_toc','set_track_changes','apply_track_changes','set_paragraph_layout',
        'set_table_format','set_object_layout',
    }
    if any(isinstance(e,dict) and e.get('type') in advanced_kinds for e in edits):
        if any(not isinstance(e,dict) or e.get('type') not in advanced_kinds for e in edits):
            fail('INVALID_EDIT','고급 문서 편집은 기존 내용 변경과 별도 요청으로 실행하세요.')
        return compile_advanced_edits(doc,edits)
    if any(isinstance(e,dict) and e.get('type')=='insert_blocks' for e in edits):
        if any(not isinstance(e,dict) for e in edits):fail('INVALID_EDIT','변경 항목은 객체여야 합니다.')
        from .fill import compile_edits as compile_fill
        return compile_fill(doc,edits)
    compiled=[]
    for edit in edits:
        if not isinstance(edit,dict):fail('INVALID_EDIT','변경 항목은 객체여야 합니다.')
        kind=edit.get('type')
        if kind not in ('replace_text','replace_equation','set_format'):fail('INVALID_EDIT','허용되지 않은 변경 종류입니다.')
        if edit.get('uncertain'):fail('UNCERTAIN_CONTENT','불명확한 수정 내용을 먼저 확인하세요.')
        target=edit.get('target_id')
        ranges=expand(doc,target)
        if kind!='set_format' and len(ranges)!=1:fail('INVALID_EDIT','내용 교체는 한 글자 구간 또는 한 수식씩 지정하세요.')
        for r in ranges:
            p=doc['paragraphs'][r['paragraph']]
            if r['kind']=='control':fail('UNSUPPORTED_LOCATION','이 개체는 서식 변경 대상이 아닙니다.')
            if not p.get('mapped',False):fail('UNMAPPED_LOCATION','실제 한글 위치가 검증되지 않은 영역입니다.')
            op=dict(r,type=kind)
            if kind=='replace_text':
                replacement=edit.get('text');old=edit.get('expected_text')
                if not isinstance(replacement,str) or any(c in replacement for c in '\r\n\x00\t') or not isinstance(old,str):
                    fail('INVALID_EDIT','expected_text와 줄바꿈 없는 text가 필요합니다.')
                pieces=[a for a in p['parts'] if a['end']>r['start'] and a['start']<r['end']]
                if not pieces or any(a['kind']!='text' for a in pieces) or pieces[0]['start']>r['start'] or pieces[-1]['end']<r['end']:
                    fail('INVALID_EDIT','개체를 가로지르는 글자 교체는 지원하지 않습니다.')
                actual=''
                for part in pieces:
                    lo=max(r['start'],part['start'])-part['start'];hi=min(r['end'],part['end'])-part['start']
                    actual+=part['text'][char_index(part['text'],lo):char_index(part['text'],hi)]
                if actual!=old:fail('STALE_TARGET','교체 전 원문이 다릅니다.')
                part=pieces[0]
                op.update(text=replacement,expected_text=old,style=part['style'])
            elif kind=='replace_equation':
                part=next((a for a in p['parts'] if a['kind']=='equation' and a['start']==r['start'] and a['end']==r['end']),None)
                if part is None or part['script']!=edit.get('expected_script'):fail('STALE_TARGET','교체 전 수식이 다릅니다.')
                fmt=edit.get('format','latex')
                if fmt not in ('latex','hwp'):fail('INVALID_EDIT','format은 latex 또는 hwp입니다.')
                op.update(script=convert(edit.get('script')) if fmt=='latex' else validate_script(edit.get('script')),expected_script=part['script'])
            else:
                props=edit.get('properties')
                allowed={'font_name','size_pt','equation_font','equation_size_pt','bold','italic','underline','strikethrough','text_color'}
                if not isinstance(props,dict) or not props or set(props)-allowed:fail('INVALID_FORMAT','지원되지 않는 서식 속성이 있습니다.')
                for key,value in props.items():
                    if key.endswith('_pt'):
                        if isinstance(value,bool) or not isinstance(value,(int,float)) or not 0.1<=value<=4096 or round(value*100)!=value*100:
                            fail('INVALID_FORMAT','크기는 0.1~4096pt, 소수점 두 자리 이내여야 합니다.')
                    elif key in ('bold','italic','underline','strikethrough'):
                        if not isinstance(value,bool):fail('INVALID_FORMAT','불리언 서식 값이 필요합니다.')
                    elif key=='text_color':
                        if not isinstance(value,str) or not re.fullmatch(r'#[0-9A-Fa-f]{6}',value):fail('INVALID_FORMAT','text_color는 #RRGGBB 형식이어야 합니다.')
                    elif not isinstance(value,str) or not value.strip():fail('INVALID_FORMAT','유효한 글꼴 이름이 필요합니다.')
                for part in p['parts']:
                    if part['kind']=='equation' and max(r['start'],part['start'])<min(r['end'],part['end']) and not r['start']<=part['start']<part['end']<=r['end']:
                        fail('PARTIAL_CONTROL','수식 개체의 일부만 서식 변경할 수 없습니다.')
                op['properties']=props
            compiled.append(op)
    grouped={}
    for op in compiled:grouped.setdefault(op['paragraph'],[]).append(op)
    for group in grouped.values():
      for i,a in enumerate(group):
        for b in group[i+1:]:
            if a['paragraph']!=b['paragraph'] or max(a['start'],b['start'])>=min(a['end'],b['end']):continue
            if (a['type']=='set_format')==(b['type']=='set_format'):fail('OVERLAPPING_EDITS','겹치는 내용 변경 또는 서식 변경을 하나로 합치세요.')
            style=a if a['type']=='set_format' else b;content=b if a['type']=='set_format' else a
            if not style['start']<=content['start']<content['end']<=style['end']:fail('OVERLAPPING_EDITS','서식 범위가 내용 교체 구간의 일부와 겹칩니다.')
    if sum(len(o.get('text',''))+len(o.get('script','')) for o in compiled)>200000:fail('INVALID_EDIT','한 번의 내용 변경은 200,000자 이내여야 합니다.')
    return compiled

def char_index(text,offset):
    count=0
    for i,c in enumerate(text):
        if count==offset:return i
        count+=units(c)
    if count==offset:return len(text)
    fail('INVALID_EDIT','문자 중간의 위치는 선택할 수 없습니다.')

def atoms(p):
    result=[]
    if not p['parts']:
        return [{'kind':'empty','style':copy.deepcopy(p.get('empty_style',{})),'start':0,'end':0}]
    for part in p['parts']:
        if part['kind']=='text':
            start=part['start']
            for c in part['text']:
                result.append({'kind':'text','text':c,'style':part['style'],'start':start,'end':start+units(c)})
                start+=units(c)
        else:result.append({k:copy.deepcopy(v) for k,v in part.items() if k!='id'})
    return result

def shifted(position,ops):
    return position+sum(units(o['text'])-(o['end']-o['start']) for o in ops if o['type']=='replace_text' and o['end']<=position)

def expected_atoms(doc,ops):
    result=[atoms(p) for p in doc['paragraphs']]
    for op in sorted((o for o in ops if o['type']!='set_format'),key=lambda o:(o['paragraph'],o['start']),reverse=True):
        aa=result[op['paragraph']]
        indexes=[i for i,a in enumerate(aa) if op['start']<=a['start'] and a['end']<=op['end']]
        if op['type']=='replace_text':
            new=[{'kind':'text','text':c,'style':op['style'],'start':op['start'],'end':op['end']} for c in op['text']]
            aa[indexes[0]:indexes[-1]+1]=new
            if not aa:aa.append({'kind':'empty','style':op['style'],'start':0,'end':0})
        else:aa[indexes[0]]['script']=op['script']
    # Apply formatting using original atom coordinates, before stripping them.
    for op in (o for o in ops if o['type']=='set_format'):
        style_cache={}
        for a in result[op['paragraph']]:
            if a['kind']!='empty' and not op['start']<=a['start']<a['end']<=op['end']:continue
            props=op['properties']
            if a['kind'] in ('text','empty'):
                key=id(a['style'])
                if key not in style_cache:
                    changed=dict(a['style'])
                    if 'size_pt' in props:changed['size_pt']=props['size_pt']
                    if 'font_name' in props:changed['fonts']={k:props['font_name'] for k in LANGS}
                    for key in ('bold','italic','underline','strikethrough','text_color'):
                        if key in props:changed[key]=props[key]
                    style_cache[key]=changed
                a['style']=style_cache[key]
            elif a['kind']=='equation':
                if 'equation_size_pt' in props:a['size_pt']=props['equation_size_pt']
                if 'equation_font' in props:a['font']=props['equation_font']
    return [[{k:v for k,v in a.items() if k not in ('start','end')} for a in aa] for aa in result]

def verify_edit(before,after,ops):
    advanced_kinds={
        'set_page_layout','edit_table','set_header_footer','insert_note','insert_comment','insert_field',
        'insert_textbox','break_section','set_page_number','generate_index','generate_toc','set_track_changes',
        'apply_track_changes','set_paragraph_layout','set_table_format','set_object_layout',
    }
    if any(o['type'] in advanced_kinds for o in ops):
        if before['revision']==after['revision']:
            fail('VERIFY_FAILED','고급 문서 편집이 적용되지 않았습니다.')
        if before['revision']==after['revision'] and any(o['type']!='set_header_footer' for o in ops):
            fail('VERIFY_FAILED','문서 구조 또는 페이지 설정이 변경되지 않았습니다.')
        for o in ops:
            if o['type']=='edit_table':
                if before['table_structure']==after['table_structure']:
                    fail('VERIFY_FAILED','표 구조가 요청대로 변경되지 않았습니다.')
            elif o['type']=='set_page_layout':
                if before['revision']==after['revision']:
                    fail('VERIFY_FAILED','쪽 여백 또는 용지 설정이 변경되지 않았습니다.')
            elif o['type']=='set_header_footer':
                if before['revision']==after['revision']:
                    fail('VERIFY_FAILED','머리글 또는 바닥글이 변경되지 않았습니다.')
        return
    if any(o['type']=='insert_blocks' for o in ops):
        from .fill import verify
        return verify(before,after,ops)
    if before['table_structure']!=after['table_structure']:fail('VERIFY_FAILED','기존 표 구조가 달라졌습니다.')
    if before['bookmarks']!=after['bookmarks']:fail('VERIFY_FAILED','책갈피 구성이 달라졌습니다.')
    if len(before['paragraphs'])!=len(after['paragraphs']):fail('VERIFY_FAILED','문단 개수가 달라졌습니다.')
    for a,b in zip(before['paragraphs'],after['paragraphs']):
        for key in ('scope','cell_path','paragraph_shape','style','attributes'):
            if a[key]!=b[key]:fail('VERIFY_FAILED','요청 밖 문단 서식 또는 위치가 바뀌었습니다: '+key)
    if expected_atoms(before,ops)!=expected_atoms(after,[]):fail('VERIFY_FAILED','요청 밖 내용·수식·글자 서식이 바뀌었거나 요청한 변경이 적용되지 않았습니다.')
