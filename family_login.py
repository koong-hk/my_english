import os
import time

import streamlit as st

from daily_service import DailyService


def require_family_login():
    config = dict(os.environ)
    try:
        config.update(dict(st.secrets))
    except FileNotFoundError:
        pass
    account = st.session_state.get('family_account')
    if account and time.time() - st.session_state.get('family_activity', 0) > 1800:
        st.session_state.clear()
        st.rerun()
    if not account:
        st.title('우리 가족 영어 학습')
        with st.form('family_login', clear_on_submit=True):
            profile = st.radio('계정 선택', ['father', 'son'],
                               format_func=lambda p: {'father': '아버지', 'son': '아들'}[p],
                               horizontal=True)
            pin = st.text_input('숫자 4자리 PIN', type='password', max_chars=4)
            submitted = st.form_submit_button('로그인', type='primary')
        if submitted:
            if len(pin) != 4 or not pin.isascii() or not pin.isdigit():
                st.warning('숫자 4자리를 입력해 주세요.')
            else:
                try:
                    valid = DailyService(config).request('POST', 'rpc/verify_english_pin',
                        {'account_id': profile, 'supplied_pin': pin})
                    if valid:
                        st.session_state.clear()
                        st.session_state.family_account = profile
                        st.session_state.family_activity = time.time()
                        st.rerun()
                    st.error('PIN을 확인해 주세요. 5회 실패하면 해당 계정은 15분 동안 잠깁니다.')
                except (RuntimeError, ValueError):
                    st.error('로그인 설정을 확인해 주세요. schema.sql 실행과 PIN 등록이 필요합니다.')
        st.stop()
    st.session_state.family_activity = time.time()
    with st.sidebar:
        st.write('로그인 계정 · ' + {'father': '아버지', 'son': '아들'}[account])
        if st.button('로그아웃 · 계정 변경'):
            st.session_state.clear()
            st.rerun()
    return account
