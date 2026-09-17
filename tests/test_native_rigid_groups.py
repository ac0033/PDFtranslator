import random
from pdf_translation.native_rigid_flow import overlap_roots


def test_batched_column_sweep_matches_full_overlap_graph():
    randomizer=random.Random(4107)
    items=[]
    for _ in range(250):
        y=randomizer.randrange(100);height=randomizer.randrange(1,25)
        left,right=randomizer.choice([(0,100),(110,150),(0,150),(20,90),(30,40)])
        items.append({'source':[left,y,right,y+height],'flow':[left,right],'pinned':bool(randomizer.randrange(4)==0)})
    neighbors=[set() for _ in items]
    for i,a in enumerate(items):
        for j,b in enumerate(items[:i]):
            if a['pinned']==b['pinned'] and min(a['flow'][1],b['flow'][1])>max(a['flow'][0],b['flow'][0]) and max(a['source'][1],b['source'][1])<=min(a['source'][3],b['source'][3])+.5:
                neighbors[i].add(j);neighbors[j].add(i)
    expected=[];seen=set()
    for i in range(len(items)):
        if i in seen:continue
        group=set();todo=[i]
        while todo:
            j=todo.pop()
            if j in group:continue
            group.add(j);todo.extend(neighbors[j]-group)
        seen.update(group);expected.append(frozenset(group))
    roots=overlap_roots(items);actual={}
    for i,root in enumerate(roots):actual.setdefault(root,set()).add(i)
    assert set(map(frozenset,actual.values()))==set(expected)


def test_dense_vector_column_uses_one_connected_component():
    items=[{'source':[0,0,100,100],'flow':[0,100],'pinned':False} for _ in range(10000)]
    assert len(set(overlap_roots(items)))==1


def test_rounding_never_turns_a_nonnegative_gap_into_overlap():
    from pdf_translation.native_rigid_flow import layout_groups
    items=[{'kind':'translation','index':0,'source':[0,0,100,10],'target':[0,0,100,9.35],'connection_bottom':9,'flow':[0,100],'pinned':False},
           {'kind':'glyph','index':1,'source':[0,10.1,100,20],'target':[0,10.1,100,20],'flow':[0,100],'pinned':False}]
    groups=layout_groups(items)
    assert len(groups)==2
    assert groups[1]['output'][1]>=groups[0]['output'][3]
    assert abs(groups[1]['gaps'][0]['delta'])<1
