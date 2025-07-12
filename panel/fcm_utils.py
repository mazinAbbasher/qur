import requests
import json
from google.oauth2 import service_account
from google.auth.transport.requests import Request
from django.contrib.auth import get_user_model

# Path to your Firebase service account JSON
FIREBASE_SERVICE_ACCOUNT_FILE = '/home/mazin/projects/maha/firebase_service_account.json'
FCM_PROJECT_ID = 'alsinary-973fa'  # Replace with your project ID

def get_access_token():
    credentials = service_account.Credentials.from_service_account_file(
        FIREBASE_SERVICE_ACCOUNT_FILE,
        scopes=["https://www.googleapis.com/auth/firebase.messaging"]
    )
    credentials.refresh(Request())
    print("Access token obtained successfully.")
    print(f"Access token: {credentials.token}")  # Debugging line to check the token
    return credentials.token

def send_fcm_notification_to_staff(title, body, data=None, order_id=None, type="house"):
    print("Preparing to send FCM notification to staff...")
    User = get_user_model()
    staff_users = User.objects.filter(is_active=True).filter(is_staff=True) | User.objects.filter(is_superuser=True)
    print(f"Found {staff_users.count()} staff users to send notifications to.")
    if not staff_users.exists():
        print("No staff users found to send notifications.")
        return
    tokens = []
    for user in staff_users.distinct():
        token = getattr(getattr(user, 'profile', user), 'fcm_token', None)
        if token:
            tokens.append(token)
    if not tokens:
        print("No FCM tokens found for staff or superusers.")
        return

    access_token = get_access_token()
    url = f"https://fcm.googleapis.com/v1/projects/{FCM_PROJECT_ID}/messages:send"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json; UTF-8",
    }
    safe_data = {str(k): str(v) for k, v in (data or {}).items()}
    # if order_id:
    order_id = str(safe_data["appointment_id"])
    type = safe_data["type"]
    if type == "construction":
        order_url = f"/maha/admin-panel/show-construction-appointment/{order_id}/"
    else:
        order_url = f"/maha/admin-panel/show-appointment/{order_id}/"
    # else:
    #     order_url = "/"

    for token in tokens:
        message = {
            "message": {
                "token": token,
                "notification": {
                    "title": title,
                    "body": body,
                },
                "data": safe_data,
                "android": {
                    "priority": "HIGH",
                    "notification": {
                        "sound": "default",
                        "color": "#1c6cb8",
                        "click_action": "VIEW_ORDER",
                        "channel_id": "default_channel",
                    },
                },
                "apns": {
                    "payload": {
                        "aps": {
                            "sound": "default",
                            "category": "VIEW_ORDER",
                            "content-available": 1,
                        }
                    },
                    "headers": {
                        "apns-priority": "10",
                    }
                },
                "webpush": {
                    "headers": {
                        "Urgency": "high"
                    },
                    "notification": {
                        "icon": "/home/mazin/maha/static/img/micro.svg",
                        "badge": "/home/mazin/maha/static/img/micro.svg",
                        "click_action": order_url,
                    }
                }
            }
        }
        response = requests.post(url, headers=headers, data=json.dumps(message))
        if response.status_code == 200:
            print(f"Notification sent successfully to {token}")
        else:
            print(f"Failed to send notification to {token}: {response.status_code} - {response.text}")
        # Optionally log or handle errors here
