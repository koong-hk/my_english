import os
import time

import streamlit as st

from daily_service import DailyService


ACCOUNT_NAMES = {'guest': 'family', 'father': 'Daddy', 'son': '도윤'}


def require_family_login():
    config = dict(os.environ)
    try:
        config.update(dict(st.secrets))
    except FileNotFoundError:
        pass
    account = st.session_state.get('family_account', 'guest')
    if account != 'guest' and time.time() - st.session_state.get('family_activity', 0) > 1800:
        st.session_state.clear()
        st.rerun()
    with st.sidebar:
        selected = st.radio(
            '사용자 계정', list(ACCOUNT_NAMES),
            index=list(ACCOUNT_NAMES).index(account),
            format_func=ACCOUNT_NAMES.get, key='family_account_picker',
            horizontal=True,
        )
    if selected == 'guest':
        if account != 'guest':
            st.session_state.clear()
            st.session_state.family_account = 'guest'
            st.rerun()
        st.session_state.family_account = 'guest'
        with st.sidebar:
            st.caption('공용 계정의 학습 기록은 함께 사용합니다.')
        return 'guest'
    if selected != account:
        # A private account is assigned only after successful PIN verification.
        st.title('우리 가족 영어 학습')
        st.write(ACCOUNT_NAMES[selected] + ' 계정으로 로그인')
        with st.form('family_login_' + selected, clear_on_submit=True):
            pin = st.text_input('숫자 4자리 PIN', type='password', max_chars=4)
            submitted = st.form_submit_button('로그인', type='primary')
        if submitted:
            if len(pin) != 4 or not pin.isascii() or not pin.isdigit():
                st.warning('숫자 4자리를 입력해 주세요.')
            else:
                try:
                    valid = DailyService(config).request('POST', 'rpc/verify_english_pin',
                        {'account_id': selected, 'supplied_pin': pin})
                    if valid:
                        st.session_state.clear()
                        st.session_state.family_account = selected
                        st.session_state.family_activity = time.time()
                        st.rerun()
                    st.error('PIN을 확인해 주세요. 5회 실패하면 해당 계정은 15분 동안 잠깁니다.')
                except (RuntimeError, ValueError):
                    st.error('로그인 설정을 확인해 주세요. schema.sql 실행과 PIN 등록이 필요합니다.')
        st.stop()
    st.session_state.family_activity = time.time()
    with st.sidebar:
        st.write('로그인 계정 · ' + ACCOUNT_NAMES[account])
        if st.button('로그아웃 · 공용으로 전환'):
            st.session_state.clear()
            st.rerun()
    return account
