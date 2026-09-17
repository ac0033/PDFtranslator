from pathlib import Path
import pytest
from pdf_translation.native_asset_plan import prepare_asset_document
from pdf_translation.native_fixed_flow import expand_fixed_flow
from pdf_translation.source_preserving import SourcePreservingUnsupported


@pytest.fixture(scope='module')
def source_units():
    source=Path('pdf/cs224n-2019-notes03-neuralnets.pdf')
    if not source.exists():pytest.skip('public Stanford integration fixture not present')
    return prepare_asset_document(source)


def test_static_equation_is_retained_outside_prose_edits(source_units):
    units,plans=source_units
    unit=next(u for u in units if u.source_text.startswith('The derivative is the expressed as:'))
    marker=next(iter(unit.protected_literal_hints))
    _,flows,_,proof=expand_fixed_flow(units,plans,{unit.unit_id:'其导数表示为：'+marker})
    retained=set(proof[0]['retained_glyph_ids'])
    edited={gid for p in flows for c in p['chunks'] for s in c['slots'] for gid in s['glyph_ids']}
    assert retained and edited and not retained&edited
    assert len(flows)==1 and not flows[0]['assets']


def test_missing_fixed_equation_is_rejected(source_units):
    units,plans=source_units
    unit=next(u for u in units if u.source_text.startswith('The derivative is the expressed as:'))
    with pytest.raises(SourcePreservingUnsupported,match='reordered or omitted'):
        expand_fixed_flow(units,plans,{unit.unit_id:'其导数表示为：'})


def test_short_hyphenated_tail_is_prose(source_units):
    units,_=source_units
    unit=next(u for u in units if u.source_text.startswith('This formulation can be visualized'))
    assert 'Figure' in unit.source_text and 'Fig-' not in unit.source_text
    assert list(unit.protected_literal_hints.values())==['2']


def test_code_context_is_not_encoded_as_math_subscripts(source_units):
    units,_=source_units
    unit=next(u for u in units if u.source_text.startswith('Snippet ') and '2.1' in u.protected_literal_hints.values())
    codes=[s.marker for s in unit.protected if s.kind=='code']
    assert codes
    literal=unit.protected_literal_hints[codes[0]]
    assert 'def eval_numerical_gradient(f, x):' in literal
    assert '_{' not in literal


def test_cross_page_prose_preserves_the_complete_translation(source_units):
    from pdf_translation.native_page_flow import expand_prose_pages
    units,plans=source_units
    unit=next(u for u in units if u.source_text.startswith('We can also combine the weights'))
    text='我们也可以将上面的权重和偏置项合并，等价地表示为：'
    _,_,translated,proof=expand_prose_pages(units,plans,{unit.unit_id:text},Path('C:/Windows/Fonts/simfang.ttf'))
    assert ''.join(translated.values())==text
    assert proof[0]['content_preserved']
    assert proof[0]['parts'][1]['translated_text']==''


def test_cross_page_overflow_cannot_drop_text(source_units):
    from pdf_translation.native_page_flow import expand_prose_pages
    units,plans=source_units
    unit=next(u for u in units if u.source_text.startswith('We can also combine the weights'))
    with pytest.raises(SourcePreservingUnsupported,match='exceeds original page intervals'):
        expand_prose_pages(units,plans,{unit.unit_id:'不能丢失的文字。'*100},Path('C:/Windows/Fonts/simfang.ttf'))


def test_gray_background_asset_contains_glyphs_only(source_units):
    from pdf_translation.native_assets import NativeAssetStore
    units,plans=source_units
    unit=next(u for u in units if u.source_text.startswith('Snippet ') and '2.1' in u.protected_literal_hints.values())
    plan=next(p for p in plans if p['unit_id']==unit.unit_id)
    info=next(a for a in plan['assets'].values() if a['placement']=='inline_reflow')
    store=NativeAssetStore(Path('pdf/cs224n-2019-notes03-neuralnets.pdf'))
    try:
        asset=store.add(info['page_index'],info['glyph_ids'],info['baseline'])
        assert asset.background_rgb!=(255,255,255)
        assert store.document[asset.page_index].get_drawings()==[]
        assert store.document[asset.page_index].get_image_info()==[]
        assert store.verify_isolated_pixels(asset,4,0)['changed_pixels']==0
    finally:store.close()


def test_nontext_removal_preserves_text_string_contents():
    from pdf_translation.native_editor import strip_nontext_paint
    source=b'0 0 10 10 re f BT /F1 10 Tf (f S Do) Tj ET /Im0 Do'
    result=strip_nontext_paint(source)
    assert b'(f S Do) Tj' in result
    assert b'/Im0 Do' not in result
    assert b're  n ' in result


def test_caption_math_does_not_split_figure_word(source_units):
    units,plans=source_units
    captions=[u for u,p in zip(units,plans) if p['column']=='margin' and p['source_pages']==[7] and u.source_text.startswith('Figure')]
    assert len(captions)==2
    for unit in captions:
        assert 'F ⟦' not in unit.source_text
        assert 'δ^{(k−1)}' in unit.protected_literal_hints.values()


def test_body_overflow_word_and_new_section_are_kept_separate(source_units):
    units,plans=source_units
    matches=[(u,p) for u,p in zip(units,plans) if 'signum function' in u.source_text]
    assert len(matches)==1
    unit,plan=matches[0]
    assert 'depending on the sign of' in unit.source_text
    assert 'ReLU:' not in unit.source_text
    assert plan['source_pages']==[13]


def test_paper_title_short_italic_words_are_not_variables(source_units):
    units,_=source_units
    unit=next(u for u in units if u.source_text.startswith('Dropout is a powerful'))
    assert 'A Simple Way to Prevent' in unit.source_text
    assert 'A' not in unit.protected_literal_hints.values()
    assert 'to' not in unit.protected_literal_hints.values()


def test_split_et_al_is_complete_prose(source_units):
    units,_=source_units
    unit=next(u for u in units if u.source_text.startswith('Dropout applied'))
    assert unit.source_text.endswith('Srivastava et al.')
    assert not unit.protected_literal_hints


def test_wrapped_caption_variable_can_reflow_with_its_sentence(source_units):
    units,plans=source_units
    captions=[p for u,p in zip(units,plans) if p['column']=='margin' and p['source_pages']==[7] and u.source_text.startswith('Figure')]
    assert len(captions)==2
    assert all(a['placement']=='inline_reflow' for p in captions for a in p['assets'].values())


def test_stacked_norm_delimiters_belong_to_inline_formula(source_units):
    units,plans=source_units
    unit,plan=next((u,p) for u,p in zip(units,plans) if p['source_pages']==[11] and 'In the above formulation' in u.source_text)
    fixed=[a for a in plan['assets'].values() if a['placement']=='source_fixed']
    assert len(fixed)==1  # Only the displayed cost equation is immovable.
    norm=[a for a in plan['assets'].values() if 639 in a['glyph_ids']]
    assert len(norm)==1 and norm[0]['placement']=='inline_reflow'
    assert set(range(636,647))<=set(norm[0]['glyph_ids'])


def test_embedded_font_names_decode_context_without_reconstructing_norm(source_units):
    units,plans=source_units
    unit,plan=next((u,p) for u,p in zip(units,plans) if p['source_pages']==[11] and 'In the above formulation' in u.source_text)
    marker=next(m for m,a in plan['assets'].items() if 639 in a['glyph_ids'])
    assert unit.protected_literal_hints[marker]=='‖W^{(i)}‖_{F}'
    assert all('�' not in literal for u in units for literal in u.protected_literal_hints.values())


def test_formula_where_clauses_are_translatable_prose(source_units):
    units,plans=source_units
    clauses=[u for u,p in zip(units,plans) if 13 in p['source_pages'] and 'where' in u.source_text]
    assert len(clauses)==3
    assert all(u.unit_type!='protected' for u in clauses)


def test_inline_named_function_is_one_original_object(source_units):
    units,plans=source_units
    matches=[u for u,p in zip(units,plans) if p['source_pages']==[13] and u.source_text.startswith('The gradient of') and 'tanh(z)' in u.protected_literal_hints.values()]
    assert len(matches)==1
    assert len(matches[0].protected_literal_hints)==1


def test_leaky_relu_name_is_translatable_but_function_call_is_protected(source_units):
    units,plans=source_units
    paragraph=next(u for u,p in zip(units,plans) if p['source_pages']==[14] and 'Traditional ReLU' in u.source_text)
    assert paragraph.source_text.startswith('Leaky ReLU:')
    assert 'leaky ReLU modifies' in paragraph.source_text
    assert 'leaky' not in paragraph.protected_literal_hints.values()
    assert any('leaky(z)' in lit for u,p in zip(units,plans) if 14 in p['source_pages'] for lit in u.protected_literal_hints.values())


def test_matrix_list_and_update_equation_labels_stay_native(source_units):
    units,plans=source_units
    white=next(u for u,p in zip(units,plans) if p['source_pages']==[15] and u.source_text.startswith('Whitening'))
    assert any(lit.replace(' ','').rstrip('.')=='U,S,V' for lit in white.protected_literal_hints.values())
    update=[u for u,p in zip(units,plans) if 15 in p['source_pages'] and any('new' in lit and 'old' in lit for lit in u.protected_literal_hints.values())]
    assert len(update)==1
    plan=next(p for u,p in zip(units,plans) if u.unit_id==update[0].unit_id)
    marker=next(m for m,lit in update[0].protected_literal_hints.items() if 'new' in lit and 'old' in lit)
    assert plan['assets'][marker]['placement']=='source_fixed'
    assert 'new' not in update[0].source_text and 'old' not in update[0].source_text


def test_exponent_is_not_a_hyphenated_prose_continuation(source_units):
    units,plans=source_units
    unit=next(u for u,p in zip(units,plans) if p['source_pages']==[16] and u.source_text.startswith('Exponential decay'))
    assert 'at iteration' in unit.source_text
    assert ' kt ' not in unit.source_text and 'iter-' not in unit.source_text
    assert any('e' in lit and '−kt' in lit for lit in unit.protected_literal_hints.values())


def test_cross_page_prose_uses_source_leading_not_a_larger_default(source_units):
    from pdf_translation.native_page_flow import expand_prose_pages
    units,plans=source_units
    u,p=next((u,p) for u,p in zip(units,plans) if u.source_text.startswith('Momentum methods'))
    text='动量方法是一种受物理学中动力学与运动研究启发的梯度下降变体，它试图利用更新的“速度”作为一种更有效的更新方案。动量更新的伪代码如下所示：'
    _,newplans,_,proof=expand_prose_pages([u],[p],{u.unit_id:text},Path('C:/Windows/Fonts/simfang.ttf'))
    assert [p['flow_line_height'] for p in newplans]==[13.5,14.0]
    assert proof[0]['content_preserved'] is True
    assert ''.join(part['translated_text'] for part in proof[0]['parts'])==text


def test_inline_formula_connector_retains_both_complete_formulas(source_units,tmp_path):
    import pymupdf
    from pdf_translation.native_asset_layout import render_asset_page
    from pdf_translation.native_editor import inspect_native_page
    units,plans=source_units
    u,p=next((u,p) for u,p in zip(units,plans) if p['source_pages']==[17] and p['rows'][0].get('inline_connector'))
    markers=list(u.protected_literal_hints)
    assert len(markers)==2 and all(a['placement']=='source_fixed' for a in p['assets'].values())
    source=Path('pdf/cs224n-2019-notes03-neuralnets.pdf');target=tmp_path/'connector.pdf'
    proof=render_asset_page(source,units,plans,{u.unit_id:markers[0]+'其中'+markers[1]},16,target,Path('C:/Windows/Fonts/simfang.ttf'))
    assert proof['passed'] and not proof['assets']
    with pymupdf.open(source) as original,pymupdf.open(target) as final:
        native=inspect_native_page(original[16]);ids=proof['removed_glyph_ids']
        assert ''.join(native.glyphs[i].text for i in sorted(ids))=='where'
        from pdf_translation.native_editor import glyph_inventory,trace_inventory,inventories_equal
        from pdf_translation.source_preserving import _changed_pixels_outside
        source_fonts={g.font for g in native.glyphs}
        actual={k:v for k,v in trace_inventory(final[0]).items() if k[0] in source_fonts}
        assert inventories_equal(glyph_inventory([g for g in native.glyphs if g.index not in ids]),actual)
        masks=[pymupdf.Rect(native.glyphs[i].ink_bounds)+(-.5,-.5,.5,.5) for i in ids]
        font=pymupdf.Font(fontfile='C:/Windows/Fonts/simfang.ttf');box=font.bbox
        for span in final[0].get_texttrace():
            if span['font'] in source_fonts:continue
            for char in span['chars']:
                x,y=char[2];z=span['size'];masks.append(pymupdf.Rect(x+box.x0*z-.5,y-box.y1*z-.5,x+box.x1*z+.5,y-box.y0*z+.5))
        assert _changed_pixels_outside(original[16],final[0],masks)[0]==0
    with pytest.raises(SourcePreservingUnsupported,match='original frame'):
        render_asset_page(source,units,plans,{u.unit_id:markers[0]+'这是一段不应在原位塞入的过长翻译'+markers[1]},16,tmp_path/'overflow.pdf',Path('C:/Windows/Fonts/simfang.ttf'))


def test_original_closing_parenthesis_stays_with_its_formula(source_units,tmp_path):
    from pdf_translation.native_asset_layout import render_asset_page
    units,plans=source_units
    u,p=next((u,p) for u,p in zip(units,plans) if p['source_pages']==[8] and u.source_text.startswith('This can easily'))
    m=list(u.protected_literal_hints)
    text='这可以很容易地推广到矩阵形式：'+m[0]+'在上述公式中，'+m[1]+'运算符表示向量的逐元素乘积('+m[2]+'：'+m[3]+m[4]+'。'
    previous=next(u for u,p in zip(units,plans) if p['source_pages']==[8] and u.source_text.startswith('Now, we will'))
    n=list(previous.protected_literal_hints)
    previous_text='现在，我们来看如何计算误差向量'+n[0]+'。我们之前利用图'+n[1]+'确定了'+n[2]+'。'
    proof=render_asset_page(Path('pdf/cs224n-2019-notes03-neuralnets.pdf'),units,plans,{u.unit_id:text,previous.unit_id:previous_text},7,tmp_path/'closing.pdf',Path('C:/Windows/Fonts/simfang.ttf'))
    assert proof['passed'], proof
    formula=next(a for a in proof['assets'] if a['glyph_ids']==p['assets'][m[3]]['glyph_ids'])
    closing=next(a for a in proof['assets'] if a['glyph_ids']==p['assets'][m[4]]['glyph_ids'])
    assert u.protected_literal_hints[m[4]]==')'
    assert formula['dy']==closing['dy']
    assert all(a['changed_pixels']==0 for a in proof['assets'])


def test_wrapped_local_gradient_function_is_one_inline_asset(source_units):
    units,plans=source_units
    u,p=next((u,p) for u,p in zip(units,plans) if p['source_pages']==[7] and 'local gradient' in u.source_text)
    marker=next(m for m,v in u.protected_literal_hints.items() if v.startswith('f'))
    assert p['assets'][marker]['placement']=='inline_reflow'
    assert len(p['assets'][marker]['glyph_ids'])==11
    assert u.source_text.endswith(marker+' .')
    assert 'k−1' in u.protected_literal_hints[marker]
    assert all(a['placement']=='inline_reflow' for a in p['assets'].values())


def test_frontmatter_font_hierarchy_and_final_prose_word(source_units):
    units,plans=source_units
    title,plan=next((u,p) for u,p in zip(units,plans) if 1 in p['source_pages'] and 'Natural Language Processing' in u.source_text)
    assert 'Lecture Notes' not in title.source_text
    assert min(s['source_font_size'] for c in plan['chunks'] if c['kind']=='prose' for s in c['slots'])>14
    subtitle=next(u for u in units if u.source_text.startswith('Lecture Notes:'))
    assert 'III' in subtitle.protected_literal_hints.values()
    assert 'Winter' not in subtitle.source_text
    fact=next(u for u in units if u.source_text.startswith('Neural networks are biologically'))
    assert fact.source_text.endswith('between the two.')
    assert not fact.protected_literal_hints


def test_colon_continuation_keeps_complete_introduction(source_units):
    units,plans=source_units
    overview=next(u for u in units if u.source_text.startswith('This set of notes introduces'))
    assert 'involving: neuron units' in overview.source_text
    assert overview.source_text.endswith('language model.')


def test_wrapped_prose_before_activation_list_is_not_protected(source_units):
    units,plans=source_units
    u=next(u for u,p in zip(units,plans) if p['source_pages']==[2] and u.source_text.startswith('If we refer'))
    assert 'activations are ' in u.source_text
    assert all('are' not in literal for literal in u.protected_literal_hints.values())


@pytest.mark.parametrize('lead',['沿用前例，将真实标注窗口的得分记为','第一种情况的 score 为','该窗口的分数为'])
def test_mixed_cross_page_parent_preserves_content_and_original_asset_pages(source_units,tmp_path,lead):
    from pdf_translation.native_page_flow import expand_prose_pages
    from pdf_translation.native_asset_layout import render_asset_page
    units,plans=source_units
    u,p=next((u,p) for u,p in zip(units,plans) if u.source_text.startswith('Using the previous example'))
    m=list(u.protected_literal_hints)
    text=lead+m[0]+'，将错误标注窗口的得分记为'+m[1]+'（下标'+m[2]+'表示被扰乱的窗口）。'
    source=Path('pdf/cs224n-2019-notes03-neuralnets.pdf');font=Path('C:/Windows/Fonts/simfang.ttf')
    newu,newp,t,proof=expand_prose_pages([u],[p],{u.unit_id:text},font,source)
    assert ''.join(part['translated_text'] for part in proof[0]['parts'])==text
    for a,b in zip(newu,newp):
        assert all(x['page_index']==b['source_pages'][0]-1 for x in b['assets'].values())
        assert set(a.protected_literal_hints)==set(b['assets'])
        assert all(t[a.unit_id].count(marker)==1 for marker in b['assets'])
    for page in (2,3):
        result=render_asset_page(source,[u],[p],{u.unit_id:text},page,tmp_path/f'mixed-{page}.pdf',font)
        assert result['passed'] and result['saved_source_glyph_inventory_verified']
        assert all(a['changed_pixels']==0 for a in result['assets'])
        assert all(a['changed_pixels']==0 for a in result['rigid_pixel_proof'])
        assert all(gap['delta']>=-.51 for group in result['vertical_spacing'] for gap in group['gaps'])
    for bad in [text.replace(m[0],''),text.replace(m[0],'SWAP').replace(m[1],m[0]).replace('SWAP',m[1]), '过长正文'*100+text]:
        with pytest.raises(SourcePreservingUnsupported):
            expand_prose_pages([u],[p],{u.unit_id:bad},font,source)


def test_wrapped_number_adjective_hyphen_is_not_a_formula_asset(source_units):
    units,plans=source_units
    u=next(u for u in units if u.source_text.startswith('Analysis of Dimensions'))
    assert '-' not in u.protected_literal_hints.values()
    assert ' - dimensional' in u.source_text
    assert list(u.protected_literal_hints.values())[:2]==['4','5']


def test_function_and_matrix_factors_remain_whole_original_equations(source_units):
    units,plans=source_units
    u=next(u for u in units if u.source_text.startswith('In the above formulation'))
    assert len(u.protected_literal_hints)==2
    assert all('f(' in t.replace(' ','') and 'Wx' in t for t in u.protected_literal_hints.values())
    assert ' f ' not in u.source_text and ' Wx ' not in u.source_text
    assert ' and ' in u.source_text


def test_nested_fraction_subscripts_stay_in_inline_object(source_units):
    units,plans=source_units
    u,p=next((u,p) for u,p in zip(units,plans) if u.source_text.startswith('Therefore we will work'))
    assert len(u.protected_literal_hints)==1
    assert 'ij' in next(iter(u.protected_literal_hints.values()))
    assert u.source_text.endswith('Thus,')
    assert len(next(iter(p['assets'].values()))['glyph_ids'])==9


def test_inline_fraction_preserves_vector_bar_and_nested_scripts(source_units,tmp_path):
    import pymupdf
    from pdf_translation.native_asset_layout import render_asset_page
    from pdf_translation.pdf_fidelity import compare_visual_primitives
    units,plans=source_units
    u,p=next((u,p) for u,p in zip(units,plans) if u.source_text.startswith('Therefore we will work'))
    marker=next(iter(u.protected_literal_hints));assert p['inline_fixed_assets']
    assert p['assets'][marker]['placement']=='source_fixed'
    source=Path('pdf/cs224n-2019-notes03-neuralnets.pdf');font=Path('C:/Windows/Fonts/simfang.ttf')
    for i,(prefix,suffix) in enumerate([('因此，简单起见，使用','进行推导。于是：'),('为简便，采用','。因此：')]):
        output=tmp_path/f'fraction-{i}.pdf'
        proof=render_asset_page(source,[u],[p],{u.unit_id:prefix+marker+suffix},4,output,font)
        assert proof['passed'] and not proof['assets'] and proof['saved_source_glyph_inventory_verified']
        assert not set(p['assets'][marker]['glyph_ids'])&set(proof['removed_glyph_ids'])
        with pymupdf.open(source) as a,pymupdf.open(output) as b:
            assert compare_visual_primitives(a[4],b[0])['checks']['vector_paths_equal']
            chinese=[span for span in b[0].get_texttrace() if span['font']=='FangSong']
            assert chinese and all(abs(char[2][1]-p['rows'][0]['baseline'])<.01 for span in chinese for char in span['chars'])
    with pytest.raises(SourcePreservingUnsupported,match='original frame'):
        render_asset_page(source,[u],[p],{u.unit_id:'过长译文'*30+marker+'于是'},4,tmp_path/'overflow.pdf',font)


def test_body_and_margin_list_bullets_are_native_assets(source_units):
    units,plans=source_units
    found=[]
    for u,p in zip(units,plans):
        if p['source_pages']!=[5] or u.unit_type=='protected':continue
        if 'receives the scalar input' not in u.source_text and 'backpropagated error calculated' not in u.source_text:continue
        marker=next(iter(u.protected_literal_hints))
        assert u.protected_literal_hints[marker]=='•'
        assert u.source_text.startswith(marker) and '•' not in u.source_text
        assert len(p['assets'][marker]['glyph_ids'])==1
        found.append(p['column'])
    assert 'body' in found and 'margin' in found



def test_short_prose_reclaims_space_before_following_original_objects(source_units,tmp_path):
    from pdf_translation.native_asset_layout import render_asset_page
    units,plans=source_units
    u,p=next((u,p) for u,p in zip(units,plans) if u.unit_type!='protected' and p['source_pages']==[3] and p['column']=='body' and not p['assets'] and len(p['rows'])>=3)
    proof=render_asset_page(Path('pdf/cs224n-2019-notes03-neuralnets.pdf'),[u],[p],{u.unit_id:'这是一段用于排版测试的短文本。'},2,tmp_path/'short.pdf',Path('C:/Windows/Fonts/simfang.ttf'))
    assert proof['passed']
    assert any(group['dy']<-10 and group['glyph_ids'] for group in proof['vertical_spacing'])
    assert all(p['changed_pixels']==0 for p in proof['rigid_pixel_proof'])
    assert all(abs(gap['delta'])<=.51 for group in proof['vertical_spacing'] for gap in group['gaps'])
