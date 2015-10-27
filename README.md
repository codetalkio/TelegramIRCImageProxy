# TelegramIRCImageProxy

A [Telegram](http://telegram.me/) bot 
that accepts photos (and images), 
uploads them to <http://imgur.com>
and posts the URL to an IRC channel.
Intended for quickly sharing images from mobile phones.

Reference configuration: 
Send photos or image files to https://telegram.me/codetalkircbot
and they will be posted 
in [#lobby on irc.codetalk.io](irc://irc.codetalk.io/lobby).
You need to authenticate before however.


## Installation

```
pip install -r requirements.txt
```

### Configuration

Configuration is saved in `config.yaml`.
All available keys are pre-inserted, with some comments.

The following keys are required:

- `telegram.token`

  Create a Telegram bot 
  using the @BotFather bot (https://telegram.me/BotFather)

- `imgur.client_id` and `imgur.client_secret`

  Register an application 
  at https://api.imgur.com/oauth2/addclient.

- `imgur.refresh_token`
  
  Add the two `client_` keys 
  and run `python authenticate_imgur.py` 
  to obtain an access_token 
  and a refresh_token, 
  then insert the refresh_token into the config file.

  This is required to upload images to a user account, 
  and currently the only option.

- `irc.host` and `irc.channel`
 
  Where to post URLs to the images.


## Usage

Just run
```
python __main__.py
```
.


## Features

- Uses long polling to fetch updates from the Telegram Bot API, 
  yielding nearly instant updates.
- Optionally groups all uploaded images into an album.
- All images are cached in a database. 
  This allows to reschedule failed image uploads 
  on the next restart 
  without re-downloading the file,
  for example.
- Users on Telegram have to authenticate in the IRC channel
  in order to be able to proxy images through the bot.
