import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
from keys import smtp_pass

SMTP_SERVER = "smtp.mail.ru"
SMTP_PORT = 465
EMAIL_ADDRESS = "no-reply@checkpulse.ru"
EMAIL_PASSWORD = smtp_pass


def send_api(email: str, name: str, api_key: str):
    subject = "🎉 Добро пожаловать, агент!"

    body = f"""
    <!doctype html>
    <html amp4email>
      <head>
        <meta charset='utf-8'>
        <style amp4email-boilerplate>body{{visibility:hidden}}</style>
        <style>
          body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
            background-color: #f5f5f7;
            margin: 0;
            padding: 0;
            color: #1d1d1f;
          }}
          .background {{
            background-image: url('cid:bgimage');
            background-size: cover;
            background-position: center;
            background-repeat: no-repeat;
            width: 100%;
            height: 100%;
            padding: 40px 0;
          }}
          .container {{
            max-width: 600px;
            margin: 0 auto;
            background-color: rgba(255, 255, 255, 0.3);
            padding: 30px;
            border-radius: 12px;
            box-shadow: 0 4px 12px rgba(0, 0, 0, 0.1);
          }}
          h1 {{
            font-size: 24px;
            color: #1d1d1f;
            text-align: center;
          }}
          p {{
            font-size: 16px;
            line-height: 1.5;
          }}
          .key-box {{
            background-color: #f2f2f5;
            padding: 15px;
            border-radius: 8px;
            margin: 20px 0;
            font-family: monospace;
            font-size: 18px;
            text-align: center;
          }}
          .footer {{
            text-align: center;
            color: #888;
            font-size: 14px;
          }}
        </style>
      </head>
      <body>
        <div class="background">
          <div class="container">
            <h1>Вы успешно зарегистрировали свою машину как агента сервиса checkpulse.ru!</h1>
            <p>Ваш персональный API-ключ:</p>
            <div class="key-box">{api_key}</div>
            <p style="color: #888; text-align: center; font-size: 14px;">
              <a href="https://github.com/CULTURE-UNION" style="color: #888; text-decoration: underline;">Установить репозиторий</a>
            </p>
            <p class="footer">С любовью, команда checkpulse.ru</p>
          </div>
        </div>
      </body>
    </html>
    """

    message = MIMEMultipart("related")
    message["From"] = EMAIL_ADDRESS
    message["To"] = email
    message["Subject"] = subject

    alternative_part = MIMEMultipart("alternative")
    alternative_part.attach(MIMEText(body, "html"))
    message.attach(alternative_part)

    try:
        with open("background.jpg", "rb") as img:
            mime_image = MIMEImage(img.read())
            mime_image.add_header("Content-ID", "<bgimage>")
            mime_image.add_header("Content-Disposition", "inline", filename="image.jpg")
            message.attach(mime_image)
    except FileNotFoundError:
        print('фон потерян')

    try:
        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as server:
            server.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
            server.sendmail(EMAIL_ADDRESS, email, message.as_string())
    except Exception as e:
        print(e)
