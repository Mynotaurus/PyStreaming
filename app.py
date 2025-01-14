import argparse
import calendar
import datetime
import emoji
import os
import random
import webcolors  # type: ignore
import yaml
from flask import Flask, Request, Response, abort, jsonify, render_template, request as base_request, redirect, make_response, url_for
from flask_socketio import SocketIO, join_room  # type: ignore
from flask_cors import CORS  # type: ignore
from typing import Any, Dict, List, Optional, cast
from werkzeug.middleware.proxy_fix import ProxyFix

from data import Data


# Allow cache-busting of entire frontend for stream page and chat updates.
FRONTEND_CACHE_BUST: str = "site.1.0.0"


app = Flask(__name__)
CORS(app)
socketio = SocketIO(app, cors_allowed_origins='*')
config: Dict[str, Any] = {}


# A quick hack to teach mypy about the valid SID parameter.
class StreamingRequest(Request):
    sid: Any


request: StreamingRequest = cast(StreamingRequest, base_request)


def mysql() -> Data:
    global config
    return Data(config)


def now() -> int:
    """
    Returns the current unix timestamp in the UTC timezone.
    """
    return calendar.timegm(datetime.datetime.utcnow().timetuple())


def modified(fname: str) -> int:
    """
    Returns the modification time in the UTC timezone.
    """
    return calendar.timegm(datetime.datetime.utcfromtimestamp(os.path.getmtime(fname)).timetuple())


def first_quality() -> Optional[str]:
    global config
    qualities = config.get('video_qualities', None)
    if not qualities:
        return None
    firstq = qualities[0]
    if isinstance(firstq, str):
        return firstq
    return None


class SocketInfo:
    def __init__(self, sid: Any, ip: str, streamer: str, username: str, admin: bool, moderator: bool, muted: bool, color: int) -> None:
        self.sid = sid
        self.ip = ip
        self.streamer = streamer
        self.username = username
        self.admin = admin
        self.moderator = moderator
        self.muted = muted
        self.color = color

    @property
    def htmlcolor(self) -> str:
        color = hex(self.color)[2:]
        if len(color) < 6:
            color = ('0' * (6 - len(color))) + color
        return '#' + color


class PresenceInfo:
    def __init__(self, sid: Any, streamer: str) -> None:
        self.sid = sid
        self.streamer = streamer
        self.timestamp = now()


socket_to_info: Dict[Any, SocketInfo] = {}
socket_to_presence: Dict[Any, PresenceInfo] = {}


def users_in_room(streamer: str) -> List[Dict[str, str]]:
    return [{'username': i.username, 'type': get_type(i), 'color': i.htmlcolor} for i in socket_to_info.values() if i.streamer == streamer]


def stream_count(streamer: str) -> int:
    oldest = now() - 30
    return len([None for x in socket_to_presence.values() if x.streamer == streamer and x.timestamp >= oldest])


def stream_live(streamkey: str, quality: Optional[str] = None) -> bool:
    global config

    if quality:
        filename = f"{streamkey}_{quality}.m3u8"
    else:
        filename = streamkey + '.m3u8'
    m3u8 = os.path.join(config['hls_dir'], filename)
    if not os.path.isfile(m3u8):
        # There isn't a playlist file, we aren't live.
        return False

    delta = now() - modified(m3u8)
    if delta >= int(config['hls_playlist_length']):
        return False

    return True


def get_color(color: str) -> Optional[int]:
    color = color.strip().lower()

    if color == "random":
        # Pick a random webcolor.
        choices = [k for k in webcolors.CSS3_NAMES_TO_HEX]
        color = random.choice(choices)

    # Attempt to convert from any color specification to hex.
    try:
        color = webcolors.name_to_hex(color, spec=webcolors.CSS3)
    except ValueError:
        pass
    try:
        color = webcolors.normalize_hex(color)
    except ValueError:
        pass

    if len(color) != 7 or color[0] != '#':
        return None

    intval = int(color[1:], 16)
    if intval < 0 or intval > 0xFFFFFF:
        return None

    return intval


def get_type(user: SocketInfo) -> str:
    if user.admin:
        return "admin"
    elif user.moderator:
        return "moderator"
    else:
        return "normal"


def fetch_m3u8(streamkey: str, quality: Optional[str] = None) -> Optional[str]:
    global config

    if quality:
        filename = f"{streamkey}_{quality}"
    else:
        filename = streamkey
    m3u8 = os.path.join(config['hls_dir'], filename) + '.m3u8'
    if not os.path.isfile(m3u8):
        # There isn't a playlist file, we aren't live.
        return None

    with open(m3u8, "rb") as bfp:
        return bfp.read().decode('utf-8')


def fetch_ts(filename: str) -> Optional[bytes]:
    global config

    ts = os.path.join(config['hls_dir'], filename)
    if not os.path.isfile(ts):
        # The file doesn't exist
        return None

    with open(ts, "rb") as bfp:
        return bfp.read()


def symlink(oldname: str, newname: str) -> None:
    src = os.path.join(config['hls_dir'], oldname)
    dst = os.path.join(config['hls_dir'], newname)
    try:
        os.symlink(src, dst)
    except FileExistsError:
        pass


def clean_symlinks() -> None:
    global config

    try:
        for name in os.listdir(config['hls_dir']):
            if name not in (os.curdir, os.pardir):
                full = os.path.join(config['hls_dir'], name)
                if os.path.islink(full):
                    real = os.readlink(full)
                    if not os.path.isfile(real):
                        # This symlink points at an old file that nginx has removed.
                        # So, let's clean up!
                        os.remove(full)
    except Exception:
        # We don't want to interrupt playlist fetching due to a failure to
        # clean. If this happens the stream will pause.
        pass


@app.context_processor
def provide_globals() -> Dict[str, Any]:
    return {
        "cache_bust": f"v={FRONTEND_CACHE_BUST}",
    }


@app.route('/')
def index() -> str:
    cursor = mysql().execute(
        "SELECT `username`, `key`, `description`, `streampass` FROM streamersettings",
    )
    streamers = [
        {
            'username': result['username'],
            'live': stream_live(result['key'], first_quality()), 'count': stream_count(result['username'].lower()),
            'description': emotes(result['description']) if result['description'] else '',
            'locked': result['streampass'] is not None,
        }
        for result in cursor.fetchall()
    ]
    return render_template('index.html', streamers=streamers)


@app.route('/<streamer>/')
def stream(streamer: str) -> Response:
    cursor = mysql().execute(
        "SELECT username, streampass FROM streamersettings WHERE username = :username",
        {"username": streamer},
    )
    if cursor.rowcount != 1:
        abort(404)

    result = cursor.fetchone()

    streampass = result['streampass']
    if streampass is not None and request.cookies.get('streampass') != streampass:
        # This stream is password protected!
        return make_response(
            render_template(
                'password.html',
                streamer=result["username"],
            ),
            403
        )

    # The stream is either not password protected, or the user has already authenticated.
    global config
    qualities = config.get('video_qualities', None)
    if not qualities:
        playlists = [{"src": url_for('streamplaylist', streamer=streamer), "label": "live", "type": "application/x-mpegURL"}]
    else:
        playlists = [{"src": url_for('streamplaylistwithquality', streamer=streamer, quality=quality), "label": quality, "type": "application/x-mpegURL"} for quality in qualities]

    emojis = {
        **emoji.get_emoji_unicode_dict('en'),  # type: ignore
        **emoji.get_aliases_unicode_dict(),  # type: ignore
    }
    emojis = {key: emojis[key] for key in emojis if "__" not in key}

    cursor = mysql().execute(
        "SELECT alias, uri FROM emotes ORDER BY alias",
    )
    emotes = {f":{result['alias']}:": result['uri'] for result in cursor.fetchall()}
    icons = {
        'admin': url_for('static', filename='admin.png'),
        'moderator': url_for('static', filename='moderator.png'),
    }

    return make_response(
        render_template(
            'stream.html',
            streamer=result["username"],
            playlists=playlists,
            emojis=emojis,
            emotes=emotes,
            icons=icons,
        )
    )


@app.route('/<streamer>/password', methods=["POST"])
def password(streamer: str) -> Response:
    streamer = streamer.lower()
    cursor = mysql().execute(
        "SELECT `username`, `streampass` FROM streamersettings WHERE username = :username",
        {"username": streamer},
    )
    if cursor.rowcount != 1:
        abort(404)

    # Verify the password.
    result = cursor.fetchone()
    streampass = result['streampass']
    if request.form.get('streampass') == streampass:
        expire_date = datetime.datetime.now()
        expire_date = expire_date + datetime.timedelta(days=1)
        response = make_response(redirect(url_for("stream", streamer=result["username"])))
        response.set_cookie("streampass", streampass, expires=expire_date)
        return response

    # Wrong password bucko!
    return make_response(
        render_template(
            'password.html',
            streamer=result["username"],
            password_invalid=True,
        ),
        403
    )


@app.route('/<streamer>/info')
def streaminfo(streamer: str) -> Response:
    streamer = streamer.lower()

    cursor = mysql().execute(
        "SELECT `username`, `streampass`, `key`, `description` FROM streamersettings WHERE username = :username",
        {"username": streamer},
    )
    if cursor.rowcount != 1:
        abort(404)

    # Doesn't cost us much, so let's clean up on the fly.
    clean_symlinks()

    result = cursor.fetchone()

    # First, verify they're even allowed to see this stream.
    streampass = result['streampass']
    if streampass is not None and request.cookies.get('streampass') != streampass:
        # This stream is password protected!
        abort(403)

    # The stream is either not password protected, or the user has already authenticated.
    live = stream_live(result['key'], first_quality())
    return make_response(jsonify({
        'live': live,
        'count': stream_count(streamer) if live else 0,
        'description': emotes(result['description']) if result['description'] else '',
    }))


@app.route('/<streamer>/playlist.m3u8')
def streamplaylist(streamer: str) -> str:
    streamer = streamer.lower()

    cursor = mysql().execute(
        "SELECT `username`, `streampass`, `key` FROM streamersettings WHERE username = :username",
        {"username": streamer},
    )
    if cursor.rowcount != 1:
        abort(404)

    result = cursor.fetchone()

    # First ensure they're even allowed to see this stream.
    streampass = result['streampass']
    if streampass is not None and request.cookies.get('streampass') != streampass:
        # This stream is password protected!
        abort(403)

    # The stream is either not password protected, or the user has already authenticated.
    key = result['key']

    if not stream_live(key):
        abort(404)

    m3u8 = fetch_m3u8(key)
    if m3u8 is None:
        abort(404)

    lines = m3u8.splitlines()
    for i in range(len(lines)):
        if lines[i].startswith(key) and lines[i][-3:] == ".ts":
            # We need to rewrite this
            oldname = lines[i]
            newname = f"{streamer}" + lines[i][len(key):]
            symlink(oldname, newname)
            lines[i] = "/hls/" + newname
        if key in lines[i]:
            raise Exception("Possible stream key leak!")

    # Doesn't cost us much, so let's clean up on the fly.
    clean_symlinks()

    m3u8 = "\n".join(lines)
    return m3u8


@app.route('/<streamer>/playlist/<quality>.m3u8')
def streamplaylistwithquality(streamer: str, quality: str) -> str:
    streamer = streamer.lower()

    cursor = mysql().execute(
        "SELECT `username`, `streampass`, `key` FROM streamersettings WHERE username = :username",
        {"username": streamer},
    )
    if cursor.rowcount != 1:
        abort(404)

    result = cursor.fetchone()

    # First ensure they're even allowed to see this stream.
    streampass = result['streampass']
    if streampass is not None and request.cookies.get('streampass') != streampass:
        # This stream is password protected!
        abort(403)

    # The stream is either not password protected, or the user has already authenticated.
    key = result['key']

    if not stream_live(key, quality):
        abort(404)

    m3u8 = fetch_m3u8(key, quality)
    if m3u8 is None:
        abort(404)

    lines = m3u8.splitlines()
    for i in range(len(lines)):
        if lines[i].startswith(key + '_' + quality) and lines[i][-3:] == ".ts":
            # We need to rewrite this
            oldname = lines[i]
            newname = f"{streamer}_{quality}" + lines[i][(len(key) + len(quality) + 1):]
            symlink(oldname, newname)
            lines[i] = "/hls/" + newname
        if key in lines[i]:
            raise Exception("Possible stream key leak!")

    # Doesn't cost us much, so let's clean up on the fly.
    clean_symlinks()

    m3u8 = "\n".join(lines)
    return m3u8


@app.route('/hls/<filename>')
def streamts(filename: str) -> Response:
    # This is a debugging endpoint only, your production nginx setup should handle this.
    ts = fetch_ts(filename)
    if ts is None:
        abort(404)

    response = make_response(ts)
    response.headers.set('Content-Type', 'video/mp2t')  # type: ignore
    return response


@app.route('/auth/on_publish', methods=["GET", "POST"])
def publishcheck() -> Response:
    key = request.values.get('name')
    if key is None:
        # We don't have a stream key, deny it.
        abort(404)

    cursor = mysql().execute(
        "SELECT `key` FROM streamersettings WHERE `key` = :key",
        {"key": key},
    )
    if cursor.rowcount != 1:
        # We didn't find a registered streamer with this key, deny it.
        abort(404)

    # This is fine, allow it
    return make_response("Stream ok!", 200)


@app.route('/auth/on_publish_done', methods=["GET", "POST"])
def donepublishcheck() -> Response:
    return make_response("Stream ok!", 200)


@socketio.on('connect')  # type: ignore
def connect() -> None:
    if request.sid in socket_to_info:
        del socket_to_info[request.sid]


@socketio.on('disconnect')  # type: ignore
def disconnect() -> None:
    if request.sid in socket_to_info:
        info = socket_to_info[request.sid]
        del socket_to_info[request.sid]

        socketio.emit('disconnected', {'username': info.username, 'type': get_type(info), 'color': info.htmlcolor, 'users': users_in_room(info.streamer)}, room=info.streamer)
    if request.sid in socket_to_presence:
        del socket_to_presence[request.sid]


@socketio.on('presence')  # type: ignore
def handle_presence(json: Dict[str, Any], methods: List[str] = ['GET', 'POST']) -> None:
    if 'streamer' not in json:
        return

    # Update user presence information
    streamer = json['streamer'].lower()
    socket_to_presence[request.sid] = PresenceInfo(request.sid, streamer)


@socketio.on('login')  # type: ignore
def handle_login(json: Dict[str, Any], methods: List[str] = ['GET', 'POST']) -> None:
    if request.sid in socket_to_info:
        socketio.emit('error', {'msg': 'SID already taken?'}, room=request.sid)
        return

    if 'username' not in json:
        socketio.emit('error', {'msg': 'Username mssing from JSON?'}, room=request.sid)
        return

    if 'streamer' not in json:
        socketio.emit('error', {'msg': 'Streamer mssing from JSON?'}, room=request.sid)
        return

    if len(json['username']) == 0:
        socketio.emit('error', {'msg': 'Username cannot be blank'}, room=request.sid)
        return

    if len(json['username']) >= 30:
        socketio.emit('error', {'msg': 'Username cannot be that long'}, room=request.sid)
        return

    streamer = json['streamer'].lower()
    username = json['username']

    for user in users_in_room(streamer):
        if user['username'].lower() == username.lower():
            socketio.emit('error', {'msg': 'Username is already taken'}, room=request.sid)
            return

    color = get_color(json['color'].strip().lower()) or 0
    key = json.get('key', None)

    # Update user presence information
    socket_to_presence[request.sid] = PresenceInfo(request.sid, streamer)

    cursor = mysql().execute(
        "SELECT `username`, `key` FROM streamersettings WHERE username = :username",
        {"username": streamer},
    )
    if cursor.rowcount != 1:
        socketio.emit('error', {'msg': 'Streamer does not exist'}, room=request.sid)
        return

    admin = False
    if username.lower() == streamer:
        result = cursor.fetchone()

        if key is None:
            socketio.emit('login key required', {'username': result['username']}, room=request.sid)
            return

        if key != result["key"]:
            socketio.emit('error', {'msg': 'Invalid password!'}, room=request.sid)
            return

        username = result['username']
        admin = True

    for _, existing in socket_to_info.items():
        if existing.streamer != streamer:
            # Not the right room
            continue

        if existing.username.lower() == json['username'].lower():
            socketio.emit('error', {'msg': 'Username is taken'}, room=request.sid)
            return

    socket_to_info[request.sid] = SocketInfo(request.sid, str(request.remote_addr), streamer, json['username'], admin, False, False, color)
    join_room(streamer)
    socketio.emit('login success', {'username': json['username']}, room=request.sid)
    socketio.emit('connected', {'username': json['username'], 'type': get_type(socket_to_info[request.sid]), 'color': socket_to_info[request.sid].htmlcolor, 'users': users_in_room(streamer)}, room=streamer)

    if admin:
        socketio.emit('server', {'msg': 'You have admin rights.'}, room=request.sid)


def emotes(msg: str) -> str:
    return emoji.emojize(emoji.emojize(msg, language="alias"), language="en")


@socketio.on('message')  # type: ignore
def handle_message(json: Dict[str, Any], methods: List[str] = ['GET', 'POST']) -> None:
    if 'message' not in json:
        socketio.emit('error', {'msg': 'Message mssing from JSON?'}, room=request.sid)
        return

    if len(json['message']) == 0:
        socketio.emit('warning', {'msg': 'Message cannot be blank'}, room=request.sid)
        return

    if request.sid not in socket_to_info:
        socketio.emit('error', {'msg': 'User is not authenticated?'}, room=request.sid)
        return

    # Update user presence information
    socket_to_presence[request.sid] = PresenceInfo(request.sid, socket_to_info[request.sid].streamer)

    message = json['message'].strip()
    if message[0] == "/":
        # Command of some sort
        if ' ' in message:
            command, message = message.split(' ', 1)
        else:
            command = message
            message = ""

        if command in ["/say"]:
            if socket_to_info[request.sid].muted:
                socketio.emit(
                    'server',
                    {'msg': "You are muted!"},
                    room=request.sid,
                )
            else:
                # Just a say message
                socketio.emit(
                    'message received',
                    {
                        'username': socket_to_info[request.sid].username,
                        'type': get_type(socket_to_info[request.sid]),
                        'color': socket_to_info[request.sid].htmlcolor,
                        'message': emotes(message),
                    },
                    room=socket_to_info[request.sid].streamer,
                )
        elif command in ["/me", "/action", "/describe"]:
            if socket_to_info[request.sid].muted:
                socketio.emit(
                    'server',
                    {'msg': "You are muted!"},
                    room=request.sid,
                )
            else:
                # An action message
                socketio.emit(
                    'action received',
                    {
                        'username': socket_to_info[request.sid].username,
                        'type': get_type(socket_to_info[request.sid]),
                        'color': socket_to_info[request.sid].htmlcolor,
                        'message': emotes(message),
                    },
                    room=socket_to_info[request.sid].streamer,
                )
        elif command in ["/color", "/setcolor"]:
            if socket_to_info[request.sid].muted:
                socketio.emit(
                    'server',
                    {'msg': "You are muted!"},
                    room=request.sid,
                )
            else:
                # Set the color of your name
                color = get_color(message.strip().lower())

                if not color:
                    socketio.emit(
                        'server',
                        {'msg': f'Invalid color {message} specified, try a color name, an HTML color like #ff00ff or "random" for a random color.'},
                        room=request.sid,
                    )
                else:
                    socket_to_info[request.sid].color = color
                    socketio.emit(
                        'action received',
                        {
                            'username': socket_to_info[request.sid].username,
                            'type': get_type(socket_to_info[request.sid]),
                            'color': socket_to_info[request.sid].htmlcolor,
                            'message': 'changed their color!',
                        },
                        room=socket_to_info[request.sid].streamer,
                    )
                    socketio.emit(
                        'return color',
                        {'color': socket_to_info[request.sid].htmlcolor},
                        room=request.sid,
                    )
        elif command in ["/name", "/nick"]:
            if socket_to_info[request.sid].muted:
                socketio.emit(
                    'server',
                    {'msg': "You are muted!"},
                    room=request.sid,
                )
            else:
                # Set a new name
                name = message.strip()

                if len(name) >= 30:
                    socketio.emit(
                        'server',
                        {'msg': 'Too long of a name specified, try a different name.'},
                        room=request.sid,
                    )
                else:
                    for user in users_in_room(socket_to_info[request.sid].streamer):
                        if user['username'].lower() == name.lower():
                            socketio.emit(
                                'server',
                                {'msg': 'Name has already been taken, try a different name.'},
                                room=request.sid,
                            )
                            break
                    else:
                        if not name:
                            socketio.emit(
                                'server',
                                {'msg': 'Invalid name specified, try a different name.'},
                                room=request.sid,
                            )
                        else:
                            old = socket_to_info[request.sid].username
                            socket_to_info[request.sid].username = name
                            socketio.emit(
                                'rename',
                                {
                                    'newname': socket_to_info[request.sid].username,
                                    'oldname': old,
                                    'type': get_type(socket_to_info[request.sid]),
                                    'color': socket_to_info[request.sid].htmlcolor,
                                    'users': users_in_room(socket_to_info[request.sid].streamer),
                                },
                                room=socket_to_info[request.sid].streamer,
                            )
        elif command in ["/help"]:
            messages = [
                "The following commands are recognized:",
                "/help - show this message",
                "/users - show the currently chatting users",
                "/me - perform an action",
                "/color - set the color of your name in chat",
                "/name - change your name to a new one",
            ]
            if socket_to_info[request.sid].admin:
                messages.append("/settings - display all stream settings")
                messages.append("/description <text> - set the stream description")
                messages.append("/password [<text>] - set or unset the stream password")
                messages.append("/mod <user> - grant moderator privileges to user")
                messages.append("/demod <user> - revoke moderator privileges to user")
            if socket_to_info[request.sid].admin or socket_to_info[request.sid].moderator:
                messages.append("/mute <user> - mute user")
                messages.append("/unmute <user> - unmute user")

            for message in messages:
                socketio.emit(
                    'server',
                    {'msg': message},
                    room=request.sid,
                )
        elif command in ["/users"]:
            socketio.emit(
                'userlist',
                {'users': users_in_room(socket_to_info[request.sid].streamer)},
                room=request.sid,
            )
        elif command in ["/settings"]:
            if not socket_to_info[request.sid].admin:
                socketio.emit(
                    'server',
                    {'msg': f"Unrecognized command '{command}', use '/help' for info."},
                    room=request.sid,
                )
                return

            streamer = socket_to_info[request.sid].streamer
            cursor = mysql().execute(
                "SELECT `description`, `streampass` FROM streamersettings WHERE `username` = :streamer",
                {"streamer": streamer}
            )
            if cursor.rowcount != 1:
                socketio.emit(
                    'server',
                    {'msg': "Error looking up settings!"},
                    room=request.sid,
                )
            else:
                result = cursor.fetchone()
                socketio.emit(
                    'server',
                    {'msg': f"Description: {result['description']}"},
                    room=request.sid,
                )
                if result['streampass']:
                    socketio.emit(
                        'server',
                        {'msg': f"Stream password: {result['streampass']}"},
                        room=request.sid,
                    )
                else:
                    socketio.emit(
                        'server',
                        {'msg': "No stream password"},
                        room=request.sid,
                    )
        elif command in ["/mute", "/quiet"]:
            if not (socket_to_info[request.sid].admin or socket_to_info[request.sid].moderator):
                socketio.emit(
                    'server',
                    {'msg': f"Unrecognized command '{command}', use '/help' for info."},
                    room=request.sid,
                )
                return

            message = message.strip().lower()
            for sinfo in socket_to_info.values():
                if sinfo.username.lower() == message and sinfo.streamer == socket_to_info[request.sid].streamer:
                    changed = (sinfo.muted is False)
                    sinfo.muted = True

                    if changed:
                        socketio.emit(
                            'server',
                            {'msg': f"User '{message}' has been muted."},
                            room=request.sid,
                        )
                        socketio.emit(
                            'server',
                            {'msg': "You have been muted."},
                            room=sinfo.sid,
                        )
                    else:
                        socketio.emit(
                            'server',
                            {'msg': f"User '{message}' is already muted."},
                            room=request.sid,
                        )
                    break
            else:
                socketio.emit(
                    'server',
                    {'msg': f"Unrecognized user '{message}'"},
                    room=request.sid,
                )
        elif command in ["/unmute", "/unquiet"]:
            if not (socket_to_info[request.sid].admin or socket_to_info[request.sid].moderator):
                socketio.emit(
                    'server',
                    {'msg': f"Unrecognized command '{command}', use '/help' for info."},
                    room=request.sid,
                )
                return

            message = message.strip().lower()
            for sinfo in socket_to_info.values():
                if sinfo.username.lower() == message and sinfo.streamer == socket_to_info[request.sid].streamer:
                    changed = (sinfo.muted is True)
                    sinfo.muted = False

                    if changed:
                        socketio.emit(
                            'server',
                            {'msg': f"User '{message}' has been unmuted."},
                            room=request.sid,
                        )
                        socketio.emit(
                            'server',
                            {'msg': "You have been unmuted."},
                            room=sinfo.sid,
                        )
                    else:
                        socketio.emit(
                            'server',
                            {'msg': f"User '{message}' is not muted."},
                            room=request.sid,
                        )
                    break
            else:
                socketio.emit(
                    'server',
                    {'msg': f"Unrecognized user '{message}'"},
                    room=request.sid,
                )
        elif command in ["/mod"]:
            if not socket_to_info[request.sid].admin:
                socketio.emit(
                    'server',
                    {'msg': f"Unrecognized command '{command}', use '/help' for info."},
                    room=request.sid,
                )
                return

            message = message.strip().lower()
            for sinfo in socket_to_info.values():
                if sinfo.username.lower() == message and sinfo.streamer == socket_to_info[request.sid].streamer:
                    changed = (sinfo.moderator is False)
                    sinfo.moderator = True

                    if changed:
                        socketio.emit(
                            'server',
                            {'msg': f"User '{message}' has been promoted to moderator."},
                            room=request.sid,
                        )
                        socketio.emit(
                            'server',
                            {'msg': "You have been promoted to moderator."},
                            room=sinfo.sid,
                        )
                    else:
                        socketio.emit(
                            'server',
                            {'msg': f"User '{message}' is already a moderator."},
                            room=request.sid,
                        )
                    break
            else:
                socketio.emit(
                    'server',
                    {'msg': f"Unrecognized user '{message}'"},
                    room=request.sid,
                )
        elif command in ["/demod", "/unmod"]:
            if not socket_to_info[request.sid].admin:
                socketio.emit(
                    'server',
                    {'msg': f"Unrecognized command '{command}', use '/help' for info."},
                    room=request.sid,
                )
                return

            message = message.strip().lower()
            for sinfo in socket_to_info.values():
                if sinfo.username.lower() == message and sinfo.streamer == socket_to_info[request.sid].streamer:
                    changed = (sinfo.moderator is True)
                    sinfo.moderator = False

                    if changed:
                        socketio.emit(
                            'server',
                            {'msg': f"User '{message}' has been demoted from moderator."},
                            room=request.sid,
                        )
                        socketio.emit(
                            'server',
                            {'msg': "You have been demoted from moderator."},
                            room=sinfo.sid,
                        )
                    else:
                        socketio.emit(
                            'server',
                            {'msg': f"User '{message}' is not a moderator."},
                            room=request.sid,
                        )
                    break
            else:
                socketio.emit(
                    'server',
                    {'msg': f"Unrecognized user '{message}'"},
                    room=request.sid,
                )
        elif command in ["/desc", "/description"]:
            if not socket_to_info[request.sid].admin:
                socketio.emit(
                    'server',
                    {'msg': f"Unrecognized command '{command}', use '/help' for info."},
                    room=request.sid,
                )
                return

            streamer = socket_to_info[request.sid].streamer
            description = emotes(message.strip())
            mysql().execute(
                "UPDATE streamersettings SET `description` = :description WHERE `username` = :streamer",
                {"streamer": streamer, "description": description}
            )

            socketio.emit(
                'server',
                {'msg': "Stream description updated!"},
                room=request.sid,
            )
        elif command in ["/password"]:
            if not socket_to_info[request.sid].admin:
                socketio.emit(
                    'server',
                    {'msg': f"Unrecognized command '{command}', use '/help' for info."},
                    room=request.sid,
                )
                return

            streamer = socket_to_info[request.sid].streamer
            if message:
                mysql().execute(
                    "UPDATE streamersettings SET `streampass` = :password WHERE `username` = :streamer",
                    {"streamer": streamer, "password": message}
                )
                socketio.emit(
                    'server',
                    {'msg': f"Stream password set to \"{message}\"!"},
                    room=request.sid,
                )
                socketio.emit(
                    'password set',
                    {
                        "password": message,
                    },
                    room=request.sid,
                )
                socketio.emit(
                    'password activated',
                    {
                        "username": streamer,
                    },
                    room=socket_to_info[request.sid].streamer,
                )
            else:
                mysql().execute(
                    "UPDATE streamersettings SET `streampass` = :password WHERE `username` = :streamer",
                    {"streamer": streamer, "password": None}
                )
                socketio.emit(
                    'server',
                    {'msg': "Stream password removed!"},
                    room=request.sid,
                )
                socketio.emit(
                    'password deactivated',
                    {
                        "username": streamer,
                        "msg": "Stream password has been removed.",
                    },
                    room=socket_to_info[request.sid].streamer,
                )
        else:
            socketio.emit(
                'server',
                {'msg': f"Unrecognized command '{command}', use '/help' for info."},
                room=request.sid,
            )
            return
    else:
        if socket_to_info[request.sid].muted:
            socketio.emit(
                'server',
                {'msg': "You are muted!"},
                room=request.sid,
            )
        else:
            socketio.emit(
                'message received',
                {
                    'username': socket_to_info[request.sid].username,
                    'type': get_type(socket_to_info[request.sid]),
                    'color': socket_to_info[request.sid].htmlcolor,
                    'message': emotes(message),
                },
                room=socket_to_info[request.sid].streamer,
            )

@socketio.on('get color')
def return_color(json: Dict[str, Any], methods: List[str] = ['GET', 'POST']) -> None:
    if request.sid not in socket_to_info:
        socketio.emit('error', {'msg': 'User is not authenticated?'}, room=request.sid)
        return

    socketio.emit(
            'return color',
            {
                'color': socket_to_info[request.sid].htmlcolor,
            },
            room=request.sid,
        )

@socketio.on('drawing')  # type: ignore
def handle_drawing(json: Dict[str, Any], methods: List[str] = ['GET', 'POST']) -> None:
    if 'src' not in json:
        socketio.emit('error', {'msg': 'Image mssing from JSON?'}, room=request.sid)
        return

    if request.sid not in socket_to_info:
        socketio.emit('error', {'msg': 'User is not authenticated?'}, room=request.sid)
        return

    # Update user presence information
    socket_to_presence[request.sid] = PresenceInfo(request.sid, socket_to_info[request.sid].streamer)

    src = json['src'].strip()

    if socket_to_info[request.sid].muted:
            socketio.emit(
                'server',
                {'msg': "You are muted!"},
                room=request.sid,
            )
    else:
        socketio.emit(
            'drawing received',
            {
                'username': socket_to_info[request.sid].username,
                'type': get_type(socket_to_info[request.sid]),
                'color': socket_to_info[request.sid].htmlcolor,
                'src': src,
            },
            room=socket_to_info[request.sid].streamer,
        )

def load_config(filename: str) -> None:
    global config

    config.update(yaml.safe_load(open(filename)))
    config['database']['engine'] = Data.create_engine(config)
    app.secret_key = config['secret_key']


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="A front end services provider for eAmusement games.")
    parser.add_argument("-p", "--port", help="Port to listen on. Defaults to 5678", type=int, default=5678)
    parser.add_argument("-d", "--debug", help="Enable debug mode. Defaults to off", action="store_true")
    parser.add_argument("-n", "--nginx-proxy", help="Number of nginx proxies in front of this server. Defaults to 0", type=int, default=0)
    parser.add_argument("-c", "--config", help="Config file to parse for instance settings. Defaults to config.yaml", type=str, default="config.yaml")
    args = parser.parse_args()

    load_config(args.config)

    if args.nginx_proxy > 0:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_host=args.nginx_proxy, x_proto=args.nginx_proxy, x_for=args.nginx_proxy)  # type: ignore
    socketio.run(app, host='0.0.0.0', port=args.port, debug=args.debug)
