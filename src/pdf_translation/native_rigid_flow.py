"""Reclaim translated paragraph height while moving complete original objects.

Overlapping source objects form rigid groups. Independent columns keep their
own predecessors; source inter-group gaps survive up to integer rounding.
"""
import math
from .native_paint import bind_nontext,clipping_scopes
from .native_columns import infer_columns
from .source_preserving import SourcePreservingUnsupported


def union_rect(rects):
    return [min(r[0] for r in rects),min(r[1] for r in rects),max(r[2] for r in rects),max(r[3] for r in rects)]


def plan_rigid_flow(page,native,selected,unit_bounds,selected_paint_ends=()):
    artwork=bind_nontext(page,native);items=[]
    for art in artwork:
        if art['end'] in selected_paint_ends:art['remove']=True
    geometry=infer_columns(page)
    def flow_frame(box,region=None):
        region=region or ('header' if box[1]<geometry['header_bottom'] or box[1]>geometry['footer_top'] else 'body' if box[0]<(geometry['body'][1]+geometry['margin'][0])/2 else 'margin')
        x0,x1=geometry[region]
        return [min(x0,box[0]),max(x1,box[2])],region=='header'
    for i,b in enumerate(unit_bounds):
        flow,pinned=flow_frame(b['source_bbox'],b['column'])
        items.append({'kind':'translation','index':i,'source':b['source_bbox'],
                      'target':[b['left'],b.get('flow_top',b['top']),b['right'],b['bottom']],'flow':flow,'pinned':pinned,
                      'connection_bottom':b['source_bbox'][3] if b.get('fixed_inline') else b.get('last_baseline',b['source_bbox'][3])})
    for glyph in native.glyphs:
        if glyph.index in selected or glyph.text.isspace():continue
        flow,pinned=flow_frame(glyph.bbox)
        items.append({'kind':'glyph','index':glyph.index,'source':list(glyph.bbox),'target':list(glyph.bbox),'flow':flow,'pinned':pinned})
    for i,art in enumerate(artwork):
        if art.get('visible') is False or art.get('remove'):continue
        flow,pinned=flow_frame(art['bbox'])
        items.append({'kind':'artwork','index':i,'source':list(art['bbox']),'target':list(art['bbox']),'flow':flow,'pinned':pinned})
    from .native_container_flow import container_groups
    items,containers=container_groups(page,native,items,unit_bounds,artwork,layout_groups)
    groups=layout_groups(items)
    expanded=[]
    for group in groups:
        regular=[]
        for item in group['members']:
            if item['kind']!='container':
                regular.append(item);continue
            container=containers[item['index']]
            parent_dy=group['dy']
            art=artwork[container['artwork_index']]
            art['dy']=parent_dy
            art['container_resize']=container['resize']
            for child in container['groups']:
                child={**child,'dy':child['dy']+parent_dy,
                       'output':[child['output'][0],child['output'][1]+parent_dy,child['output'][2],child['output'][3]+parent_dy],
                       'container':item['index']}
                expanded.append(child)
        if regular:expanded.append({**group,'members':regular})
    glyph_shifts={};unit_shifts={};evidence=[]
    for index,group in enumerate(expanded):
        dy=group['dy']
        for item in group['members']:
            if item['kind']=='translation':unit_shifts[item['index']]=dy
            elif item['kind']=='glyph':glyph_shifts[item['index']]=dy
            else:artwork[item['index']]['dy']=dy
        immutable=[m['source'] for m in group['members'] if m['kind']!='translation']
        record={'group':index,'pinned':group['pinned'],'source_bbox':group['source'],'target_bbox':group['output'],'dy':dy,'adjusted':bool(dy),
                'translated_units':[unit_bounds[m['index']]['unit_id'] for m in group['members'] if m['kind']=='translation'],
                'glyph_ids':[m['index'] for m in group['members'] if m['kind']=='glyph'],
                'artwork_indices':[m['index'] for m in group['members'] if m['kind']=='artwork'],
                'immutable_bbox':union_rect(immutable) if immutable else None,'gaps':group['gaps']}
        if 'container' in group:record['container']=group['container']
        if group['output'][1]<0 or group['output'][3]>page.rect.height:
            raise SourcePreservingUnsupported(f"reflow would place content outside the page: source={group['source']}, target={group['output']}, dy={dy}, units={record['translated_units']}, glyphs={record['glyph_ids'][:12]}, artwork={record['artwork_indices'][:12]}")
        evidence.append(record)
    for container in containers:
        art=artwork[container['artwork_index']];rect=container['resize']['source_rect'];dy=art.get('dy',0)
        if rect[1]+dy<0 or rect[3]+container['resize']['height_delta']+dy>page.rect.height:
            raise SourcePreservingUnsupported('resized source container would extend outside the page')
    for clip in sorted(clipping_scopes(native),key=lambda c:c['scope_end']-c['end']):
        shifts={glyph_shifts.get(g.index,0) for g in native.glyphs if g.index not in selected and not g.text.isspace() and clip['end']<=native.tokens[g.token_index][0]<clip['scope_end']}
        shifts.update(a.get('dy',0) for a in artwork if not a.get('remove') and a.get('visible') is not False and clip['end']<=a['start']<clip['scope_end'])
        if len(shifts)==1:
            for a in artwork:
                if a.get('visible') is False and clip['end']<=a['start']<clip['scope_end']:a['dy']=next(iter(shifts))
    return unit_shifts,glyph_shifts,artwork,evidence


def overlap_roots(items):
    parent=list(range(len(items)))
    def root(i):
        while parent[i]!=i:parent[i]=parent[parent[i]];i=parent[i]
        return i
    # Identical horizontal frames need only the active member with the latest
    # bottom edge. It represents exactly the same overlap tests, without
    # comparing every dot in a dense vector plot with every earlier dot.
    active={}
    for i in sorted(range(len(items)),key=lambda i:items[i]['source'][1]):
        item=items[i];active={key:value for key,value in active.items() if value[0]+.5>=item['source'][1]}
        for (pinned,left,right),(_,j) in active.items():
            if item['pinned']==pinned and min(item['flow'][1],right)>max(item['flow'][0],left):
                parent[root(i)]=root(j)
        key=(item['pinned'],*item['flow'])
        bottom=item.get('connection_bottom',item['source'][3])
        if key not in active or bottom>active[key][0]:active[key]=(bottom,i)
    return [root(i) for i in range(len(items))]


def layout_groups(items):
    roots=overlap_roots(items)
    members={}
    for i in range(len(items)):members.setdefault(roots[i],[]).append(items[i])
    groups=[]
    for group in members.values():
        source=union_rect([x['source'] for x in group]);target=union_rect([x['target'] for x in group])
        groups.append({'source':source,'target':target,'members':group,
                       'flow':[min(x['flow'][0] for x in group),max(x['flow'][1] for x in group)],'pinned':group[0]['pinned']})
    groups.sort(key=lambda g:(g['source'][1],g['source'][0]))
    earlier=[]
    for index,group in enumerate(groups):
        left,right=group['flow'];candidates=[g for g in earlier if not g['pinned'] and min(right,g['flow'][1])>max(left,g['flow'][0])]
        predecessors=[]
        edges=sorted({left,right,*[max(left,min(right,x)) for g in candidates for x in g['flow']]})
        for a,b in zip(edges,edges[1:]):
            covered=[g for g in candidates if g['flow'][0]<(a+b)/2<g['flow'][1]]
            if covered:
                last=max(covered,key=lambda g:g['source'][3])
                if all(last is not old for old in predecessors):predecessors.append(last)
        desired=max((g['output'][3]+max(0,group['source'][1]-g['source'][3]) for g in predecessors),default=group['target'][1])
        dy=0 if group['pinned'] else round(desired-group['target'][1])
        if not group['pinned'] and predecessors:
            minimum_top=max(g['output'][3] for g in predecessors)
            if group['target'][1]+dy<minimum_top:
                dy=math.ceil(minimum_top-group['target'][1])
        group['output']=[group['target'][0],group['target'][1]+dy,group['target'][2],group['target'][3]+dy]
        group['dy']=dy
        group['gaps']=[{'source':max(0,group['source'][1]-g['source'][3]),'target':group['output'][1]-g['output'][3],'delta':group['output'][1]-g['output'][3]-max(0,group['source'][1]-g['source'][3])} for g in ([] if group['pinned'] else predecessors)]
        earlier.append(group)
    return groups
