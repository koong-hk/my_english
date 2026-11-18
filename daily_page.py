import calendar
import hmac
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
        for col, name in zip(st.columns(7), ['월', '화', '수', '목', '금', '토', '일']):
            col.write(name)
        for week in calendar.monthcalendar(year, number):
            for col, day in zip(st.columns(7), week):
                if day:
                    iso = date(year, number, day).isoformat()
                    if col.button(f"{day}{' ●' if iso in dates else ''}", key='cloud_' + iso,
                                  use_container_width=True):
                        st.session_state.cloud_selected = iso
        st.caption('● 문제가 준비된 날짜')
        for row in month_rows:
            if st.button(f"{row['study_date']} · {row['pattern']}", key='cloud_list_' + row['study_date'],
                         use_container_width=True):
                st.session_state.cloud_selected = row['study_date']
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
