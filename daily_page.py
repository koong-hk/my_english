import calendar
import html
import os
import re
from datetime import date

import streamlit as st

from daily_service import DailyService, korean_today


def render_daily_page():
    config = dict(os.environ)
    try:
        config.update(dict(st.secrets))
    except FileNotFoundError:
        pass
    profile = st.session_state['family_account']
    st.title('Everyday Expression')
    st.caption('직접 영작한 뒤 제출하면 첨삭 결과와 학습 기록을 자동 저장합니다.')
    try:
        service = DailyService(config)
        # Fetch calendar metadata in pages so older months remain accessible.
        rows = []
        offset = 0
        while True:
            batch = service.rows('english_lessons', select='study_date,korean,pattern',
                                 order='study_date.desc', limit='500', offset=str(offset))
            rows.extend(batch)
            if len(batch) < 500:
                break
            offset += len(batch)
        months = sorted({r['study_date'][:7] for r in rows} | {korean_today()[:7]}, reverse=True)
        month = st.selectbox('학습 월', months, key='cloud_month')
        year, number = map(int, month.split('-'))
        month_rows = [r for r in rows if r['study_date'].startswith(month)]
        dates = {r['study_date'] for r in month_rows}
        # A marker represents this account's submitted study record.
        learned_dates = set()
        offset = 0
        while True:
            batch = service.rows(
                'english_submissions', select='study_date', profile_id='eq.' + profile,
                study_date='gte.' + month + '-01',
                **{'and': f'(study_date.lt.{date(year + number // 12, number % 12 + 1, 1).isoformat()})'},
                order='study_date.asc,id.asc', offset=str(offset), limit='500',
            )
            learned_dates.update(r['study_date'] for r in batch)
            if len(batch) < 500:
                break
            offset += len(batch)
        st.markdown(
            "<style>"
            ".st-key-compact_daily_calendar [data-testid='stHorizontalBlock'] {gap: 2px;}"
            ".st-key-compact_daily_calendar button {min-height: 26px; padding: 0;}"
            ".st-key-compact_daily_calendar button p {font-size: 12px; white-space: nowrap;}"
            ".st-key-compact_daily_calendar [data-testid='stExpanderDetails'] {padding: 8px 5px;}"
            "</style>", unsafe_allow_html=True,
        )
        calendar_column, list_column = st.columns([1, 3], gap='large')
        with calendar_column, st.container(key="compact_daily_calendar"):
            with st.expander('달력 · 접기 / 펼치기', expanded=True):
                marker_styles = ''.join(
                    '.st-key-calendar_day_' + day.replace('-', '_') +
                    ' button p::after {content:""; display:inline-block; width:6px;'
                    'height:6px; border-radius:50%; background:#2563eb;'
                    'margin-left:4px; vertical-align:middle;}'
                    for day in learned_dates
                )
                st.markdown('<style>' + marker_styles + '</style>', unsafe_allow_html=True)
                for col, name in zip(st.columns(7, gap='small'), ['월', '화', '수', '목', '금', '토', '일']):
                    col.markdown(f'**{name}**')
                for week in calendar.monthcalendar(year, number):
                    for col, day in zip(st.columns(7, gap='small'), week):
                        if day:
                            iso = date(year, number, day).isoformat()
                            with col:
                                with st.container(key='calendar_day_' + iso.replace('-', '_')):
                                    if st.button(str(day), key='cloud_' + iso,
                                                 use_container_width=True):
                                        st.session_state.cloud_selected = iso
                st.markdown(
                    '<span style="display:inline-block;width:6px;height:6px;'
                    'border-radius:50%;background:#2563eb;margin-right:6px"></span>'
                    '현재 계정의 답변이 저장된 날짜', unsafe_allow_html=True,
                )
        with list_column:
            if month_rows:
                table_rows = [{'날짜': r['study_date'], '영어 패턴표현': r['pattern'],
                               '한글 표현': r['korean']} for r in month_rows]
                event = st.dataframe(
                    table_rows, hide_index=True, use_container_width=True,
                    on_select='rerun', selection_mode='single-row',
                    key=f'expression_list_{profile}_{month}',
                    column_config={
                        '날짜': st.column_config.TextColumn('날짜', width='small'),
                        '영어 패턴표현': st.column_config.TextColumn('영어 패턴표현', width='large'),
                        '한글 표현': st.column_config.TextColumn('한글 표현', width='large'),
                    },
                )
                selection_key = f'last_expression_row_{profile}_{month}'
                chosen = list(event.selection.rows)
                if chosen != st.session_state.get(selection_key, []):
                    st.session_state[selection_key] = chosen
                    if chosen and chosen[0] < len(month_rows):
                        st.session_state.cloud_selected = month_rows[chosen[0]]['study_date']
            if not month_rows:
                st.info('이달에 생성된 문제가 없습니다. GitHub Actions 실행 상태를 확인해 주세요.')
        selected = st.session_state.get('cloud_selected', '')
        if not selected.startswith(month):
            selected = month_rows[0]['study_date'] if month_rows else ''
        if selected in dates:
            record = service.rows('english_lessons', study_date='eq.' + selected)[0]
            lesson = record['lesson']
            st.subheader(selected)
            st.write(lesson['korean'])
            with st.form('cloud_answer_' + selected):
                draft = st.text_area('먼저 영어로 작성해 보세요', max_chars=3000)
                submit = st.form_submit_button('제출 · 자동 첨삭 및 저장', type='primary')
            if submit:
                with st.spinner('답변을 저장하고 첨삭하고 있습니다…'):
                    result = service.submit(selected, draft, profile)
                if result:
                    st.success('첨삭 결과를 저장했습니다.')
                else:
                    st.info('처리 중인 답변입니다. 잠시 후 기록을 새로고침해 주세요.')
            with st.expander('정답 · 핵심 패턴 · 예문 · 문법 설명'):
                st.markdown('**자연스러운 구어체**')
                st.write(lesson['natural'])
                st.markdown('**격식 있는 표현**')
                st.write(lesson['formal'])
                parts = re.split(r'(\[[^\[\]]+\])', lesson['pattern'])
                markup = ''.join("<span style='color:" + ('#dc2626' if p.startswith('[') else '#15803d')
                                 + "'>" + html.escape(p) + '</span>' for p in parts)
                st.markdown(markup, unsafe_allow_html=True)
                st.caption('초록색은 고정 부분, 빨간색은 교체 부분입니다.')
                st.write(lesson['pattern_meaning'])
                st.write(lesson['explanation'])
                for i, example in enumerate(lesson['examples'], 1):
                    st.write(f"{i}. {example['english']}")
                    st.caption(example['korean'])
                for note in lesson['grammar']:
                    st.write(note)
            st.markdown('**내 답변과 피드백**')
            submissions = service.rows('english_submissions', study_date='eq.' + selected,
                                       order='created_at.desc', profile_id='eq.' + profile)
            for submission in submissions:
                with st.container(border=True):
                    st.write(submission['draft'])
                    feedback = submission.get('feedback')
                    if feedback:
                        st.write(feedback['assessment'])
                        st.success(feedback['corrected'])
                        st.write(feedback['explanation'])
                        for suggestion in feedback['suggestions']:
                            st.write(suggestion)
                    else:
                        st.info('답변이 저장되었습니다. 첨삭이 실패했다면 같은 답변을 다시 제출해 주세요.')
        if st.button('기록 새로고침'):
            st.rerun()
    except Exception as exc:
        # Never echo server credentials or provider response bodies in the page.
        st.error('연결 또는 처리에 실패했습니다. 설정과 서비스 실행 상태를 확인한 뒤 다시 시도해 주세요.')
        if isinstance(exc, ValueError):
            st.warning(str(exc))
