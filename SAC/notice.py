import requests

def send_line_notify(notification_message):
    """
    実行が終わればLINEに通知する、実行に長時間～数日とかかかるので
    """
    line_notify_token = #発行してください
    line_notify_api = 'https://notify-api.line.me/api/notify'
    headers = {'Authorization': f'Bearer {line_notify_token}'}
    data = {'message': f'message: {notification_message}'}
    requests.post(line_notify_api, headers = headers, data = data)








