"""Run locally once to set or reset family PINs without committing them."""
from getpass import getpass
from daily_service import DailyService


def main():
    url = input('Supabase project URL: ').strip()
    key = getpass('Supabase server Secret key (hidden): ').strip()
    service = DailyService({'SUPABASE_URL': url, 'SUPABASE_SECRET_KEY': key})
    for profile, label in [('father', '아버지'), ('son', '아들')]:
        pin = getpass(label + ' PIN (4 digits, hidden): ')
        if len(pin) != 4 or not pin.isascii() or not pin.isdigit():
            raise ValueError('숫자 4자리를 입력해 주세요.')
        if getpass('Confirm PIN: ') != pin:
            raise ValueError('두 PIN 입력이 다릅니다.')
        service.request('POST', 'rpc/set_english_pin', {'account_id': profile, 'new_pin': pin})
        print(label + ' PIN saved.')


if __name__ == '__main__':
    main()
