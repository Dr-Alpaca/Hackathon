"""
校园食荐 - 校园餐饮推荐应用 MVP
后端主应用

技术栈: Flask + SQLAlchemy + SQLite + 规则+LLM双轨信息提取
"""
import os
import uuid
import random
import re
import time
import json as _json
from datetime import datetime, timedelta
from werkzeug.utils import secure_filename
from flask import Flask, request, jsonify, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS

from utils import extract_info, normalize_emotion_tags

app = Flask(__name__)
CORS(app)

# 配置
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-secret-key-change-in-production')
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///campus_food.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024  # 5MB 限制
app.config['UPLOAD_FOLDER'] = 'uploads'

# 确保上传目录存在
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

db = SQLAlchemy(app)


# ========== 数据库模型 ==========

class Shop(db.Model):
    """店铺/窗口表"""
    id = db.Column(db.Integer, primary_key=True)
    canteen = db.Column(db.String(100), nullable=False)  # 食堂名称
    name = db.Column(db.String(100), nullable=False)      # 窗口名
    location_desc = db.Column(db.String(200))             # 位置描述
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('canteen', 'name', name='unique_canteen_shop'),
    )


class Post(db.Model):
    """帖子表"""
    id = db.Column(db.Integer, primary_key=True)
    device_id = db.Column(db.String(100), nullable=False)  # 设备标识
    text = db.Column(db.Text, nullable=False)              # 原始帖子内容
    images = db.Column(db.Text)                            # 图片路径，多个逗号分隔
    shop_id = db.Column(db.Integer, db.ForeignKey('shop.id'), nullable=True)
    dish_name = db.Column(db.String(100))                 # 提取的菜品名
    quote = db.Column(db.String(200))                     # 提取的评价摘录
    tags = db.Column(db.String(200))                      # 提取的标签，逗号分隔
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    shop = db.relationship('Shop', backref=db.backref('posts', lazy=True))


class Action(db.Model):
    """用户行为表（记录"我想吃"等）"""
    id = db.Column(db.Integer, primary_key=True)
    device_id = db.Column(db.String(100), nullable=False)
    target_type = db.Column(db.String(50), nullable=False)  # post / shop
    target_id = db.Column(db.Integer, nullable=False)
    action_type = db.Column(db.String(50), nullable=False)  # i_want_eat
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # 简化唯一约束：同一用户同一天对同一个目标只能点一次
    # 应用层检查日期，数据库只约束组合
    __table_args__ = (
        db.UniqueConstraint(
            'device_id', 'target_type', 'target_id', 'action_type',
            name='unique_daily_action'
        ),
    )


# ========== 工具函数 ==========

def allowed_file(filename):
    """检查文件类型是否允许"""
    ALLOWED_EXTENSIONS = {'jpg', 'jpeg', 'png', 'gif', 'webp'}
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def get_or_create_shop(canteen, shop_name):
    """查找或创建店铺"""
    if not canteen or not shop_name:
        return None

    shop = Shop.query.filter_by(canteen=canteen, name=shop_name).first()
    if shop:
        return shop

    try:
        shop = Shop(canteen=canteen, name=shop_name)
        db.session.add(shop)
        db.session.commit()
        return shop
    except Exception as e:
        db.session.rollback()
        # 唯一约束冲突，说明已经被创建了，重新查询
        return Shop.query.filter_by(canteen=canteen, name=shop_name).first()


def seed_if_empty():
    """数据库为空时写入人工种子帖，字段与线上一致，供首页推荐池抽样。"""
    if Post.query.count() > 0:
        return
    seeds = [
        {
            "text": "一食堂三楼7号窗口，铁板黑椒鸡排饭真的绝了，鸡排现煎黑椒汁给得足，12块钱还送蛋，就是排队人太多",
            "canteen": "一食堂三楼",
            "shop_name": "7号窗口",
            "dish_name": "铁板黑椒鸡排饭",
            "quote": "黑椒汁给得足，鸡排现煎",
            "tags": "量大,便宜,排队久",
        },
        {
            "text": "二食堂二楼汤面窗口菌菇鸡汤面，冷天喝一口整个人都活了，出餐很快不用等很久",
            "canteen": "二食堂二楼",
            "shop_name": "汤面窗口",
            "dish_name": "菌菇鸡汤面",
            "quote": "冷天喝一口整个人都活了",
            "tags": "出餐快,便宜",
        },
        {
            "text": "三食堂麻辣香锅窗口记得少油，不然下午课会困，辣得很爽但分量足",
            "canteen": "三食堂",
            "shop_name": "麻辣香锅窗口",
            "dish_name": "麻辣香锅",
            "quote": "辣得很爽但分量足",
            "tags": "辣,量大",
        },
        {
            "text": "四食堂照烧鸡腿饭窗口，甜度刚好，十块钱出头还要什么自行车，便宜",
            "canteen": "四食堂",
            "shop_name": "照烧鸡腿饭窗口",
            "dish_name": "照烧鸡腿饭",
            "quote": "甜度刚好，会回购",
            "tags": "便宜,量大",
        },
    ]
    for row in seeds:
        shop = get_or_create_shop(row["canteen"], row["shop_name"])
        if not shop:
            continue
        post = Post(
            device_id="seed_campus_food",
            text=row["text"],
            images=None,
            shop_id=shop.id,
            dish_name=row["dish_name"],
            quote=row["quote"],
            tags=row["tags"],
        )
        db.session.add(post)
    db.session.commit()
    print("已写入种子帖子 %s 条" % len(seeds))


# ========== API 路由 ==========

@app.route('/')
def index():
    """首页，返回前端测试页面"""
    return app.send_static_file('index.html')


@app.route('/restaurants')
def restaurants():
    """餐厅页面"""
    return app.send_static_file('restaurants.html')


@app.route('/post')
def post_page():
    """发帖页面"""
    return app.send_static_file('post.html')


@app.route('/profile')
def profile():
    """个人资料页面"""
    return app.send_static_file('profile.html')


@app.route('/uploads/<filename>')
def uploaded_file(filename):
    """获取上传的图片"""
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)


@app.route('/api/recommendations', methods=['GET'])
def get_recommendations():
    """
    获取推荐卡片
    推荐策略：
    1. 最近48小时内被发帖提及的窗口/菜品 - 60%
    2. 48小时前但累计提及次数≥3的口碑窗口 - 30%
    3. 随机抽取剩余内容（保底） - 10%
    """
    try:
        now = datetime.utcnow()
        cutoff_48h = now - timedelta(hours=48)

        # 分类查询
        # 1. 最近48小时
        recent_posts = Post.query.filter(
            Post.created_at >= cutoff_48h,
            Post.shop_id.isnot(None),
            Post.dish_name.isnot(None)
        ).order_by(Post.created_at.desc()).all()

        # 2. 口碑内容（累计提及>=3，超过48小时）
        subquery = db.session.query(
            Post.shop_id,
            db.func.count(Post.id).label('count')
        ).filter(
            Post.created_at < cutoff_48h,
            Post.shop_id.isnot(None)
        ).group_by(Post.shop_id).having(db.func.count(Post.id) >= 3).subquery()

        popular_posts = Post.query\
            .join(subquery, Post.shop_id == subquery.c.shop_id)\
            .filter(Post.shop_id.isnot(None), Post.dish_name.isnot(None))\
            .order_by(Post.created_at.desc()).all()

        # 3. 剩余所有有效帖子
        all_valid_posts = Post.query.filter(
            Post.shop_id.isnot(None),
            Post.dish_name.isnot(None)
        ).order_by(Post.created_at.desc()).all()

        # 构建候选池
        candidates = []
        candidates.extend([(p, 3) for p in recent_posts])  # 最高权重

        for p in popular_posts:
            if p not in recent_posts:
                candidates.append((p, 2))  # 中等权重

        for p in all_valid_posts:
            if not any(p == cp for cp, _ in candidates):
                candidates.append((p, 1))  # 最低权重

        # 加权抽样
        if not candidates:
            return jsonify({
                "code": 0,
                "message": "暂无推荐数据",
                "data": []
            })

        # 根据权重选择
        weights = []
        for _, weight in candidates:
            if weight == 3:
                weights.append(60)  # 60%
            elif weight == 2:
                weights.append(30)  # 30%
            else:
                weights.append(10)  # 10%

        # 抽样最多10条
        num_to_pick = min(10, len(candidates))
        selected = random.choices(
            population=list(range(len(candidates))),
            weights=weights,
            k=num_to_pick
        )

        selected_posts = [candidates[i][0] for i in selected]
        # 去重
        seen = set()
        result_posts = []
        for p in selected_posts:
            if p.id not in seen:
                seen.add(p.id)
                result_posts.append(p)

        # 格式化输出
        result = []
        for post in result_posts:
            shop = post.shop
            if not shop:
                continue

            tags = post.tags.split(',') if post.tags else []
            tags = [t.strip() for t in tags if t.strip()]

            image_url = None
            if post.images:
                first_image = post.images.split(',')[0].strip()
                if first_image:
                    image_url = f"/uploads/{first_image}"

            result.append({
                "id": post.id,
                "shop_id": shop.id,
                "canteen": shop.canteen,
                "shop_name": shop.name,
                "dish_name": post.dish_name,
                "quote": post.quote,
                "tags": tags,
                "image_url": image_url,
                "created_at": post.created_at.isoformat() if post.created_at else None
            })

        # 如果推荐池为空，提示用户发帖
        has_more = len(all_valid_posts) >= len(result)
        if not has_more and len(result) > 0:
            # 已展示所有，返回提示信息
            pass

        return jsonify({
            "code": 0,
            "message": "success",
            "data": result,
            "meta": {
                "total": len(all_valid_posts),
                "returned": len(result),
                "has_more": has_more
            }
        })

    except Exception as e:
        print(f"获取推荐失败: {e}")
        return jsonify({
            "code": 500,
            "message": str(e),
            "data": None
        }), 500


@app.route('/api/posts', methods=['GET'])
def get_posts():
    """获取所有帖子列表，按时间倒序"""
    try:
        posts = Post.query.order_by(Post.created_at.desc()).all()
        result = []
        for post in posts:
            images = []
            if post.images:
                for img in post.images.split(','):
                    img = img.strip()
                    if img:
                        images.append(f"/uploads/{img}")
            tags = []
            if post.tags:
                tags = [t.strip() for t in post.tags.split(',') if t.strip()]
            shop = post.shop
            result.append({
                "id": post.id,
                "text": post.text,
                "images": images,
                "tags": tags,
                "canteen": shop.canteen if shop else None,
                "shop_name": shop.name if shop else None,
                "dish_name": post.dish_name,
                "quote": post.quote,
                "created_at": post.created_at.isoformat() if post.created_at else None
            })
        return jsonify({"code": 0, "data": result})
    except Exception as e:
        return jsonify({"code": 500, "message": str(e)}), 500


@app.route('/api/posts', methods=['POST'])
def create_post():
    """
    创建新帖子
    接收: multipipart/form-data
    - text: 文字内容
    - images: 图片文件（多个）
    - device_id: 设备标识
    """
    try:
        device_id = request.form.get('device_id')
        text = request.form.get('text') or ''
        text_stripped = text.strip()

        if not device_id:
            return jsonify({"code": 400, "message": "缺少device_id", "data": None}), 400

        # 处理图片上传
        image_paths = []
        if 'images' in request.files:
            files = request.files.getlist('images')
            for file in files:
                if file and file.filename and allowed_file(file.filename):
                    ext = file.filename.rsplit('.', 1)[1].lower()
                    filename = f"{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}.{ext}"
                    file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
                    image_paths.append(filename)

        # 纯图片：无文字则无法提取，直接过滤
        if not text_stripped:
            return jsonify({
                "code": 400,
                "message": "纯图片帖子无法提取食堂与菜品信息，请补充文字说明",
                "data": None
            }), 400

        if len(text_stripped) < 3:
            return jsonify({"code": 400, "message": "文字至少 3 个字，或与配图一起说明食堂/窗口/菜品", "data": None}), 400

        images_str = ','.join(image_paths) if image_paths else None

        # 使用规则+LLM 提取结构化信息
        info = extract_info(text_stripped)
        if info is None:
            return jsonify({
                "code": 400,
                "message": "未能识别有效信息，请写明食堂名、窗口号或推荐菜（如：一食堂三楼 7号窗口 铁板饭）",
                "data": None
            }), 400

        canteen = info.get('canteen')
        shop_name = info.get('shop_name')
        dish_name = info.get('dish_name')
        quote = info.get('quote')
        tags = normalize_emotion_tags(info.get('tags') or [])

        # 查找或创建店铺
        shop = None
        if canteen and shop_name:
            shop = get_or_create_shop(canteen, shop_name)

        # 创建帖子
        post = Post(
            device_id=device_id,
            text=text_stripped,
            images=images_str,
            shop_id=shop.id if shop else None,
            dish_name=dish_name,
            quote=quote,
            tags=','.join(tags) if tags else None
        )

        db.session.add(post)
        db.session.commit()

        return jsonify({
            "code": 0,
            "message": "success",
            "data": {
                "post_id": post.id,
                "extracted": info
            }
        })

    except Exception as e:
        db.session.rollback()
        print(f"创建帖子失败: {e}")
        return jsonify({
            "code": 500,
            "message": str(e),
            "data": None
        }), 500


@app.route('/api/shops/<int:shop_id>', methods=['GET'])
def get_shop_detail(shop_id):
    """获取店铺详情和该店铺下的所有帖子"""
    try:
        shop = Shop.query.get(shop_id)
        if not shop:
            return jsonify({
                "code": 404,
                "message": "店铺不存在",
                "data": None
            }), 404

        posts = Post.query\
            .filter_by(shop_id=shop_id)\
            .order_by(Post.created_at.desc())\
            .all()

        posts_data = []
        for post in posts:
            tags = post.tags.split(',') if post.tags else []
            tags = [t.strip() for t in tags if t.strip()]

            images = []
            if post.images:
                for img in post.images.split(','):
                    img = img.strip()
                    if img:
                        images.append(f"/uploads/{img}")

            posts_data.append({
                "id": post.id,
                "text": post.text,
                "dish_name": post.dish_name,
                "quote": post.quote,
                "tags": tags,
                "images": images,
                "created_at": post.created_at.isoformat() if post.created_at else None
            })

        return jsonify({
            "code": 0,
            "message": "success",
            "data": {
                "shop": {
                    "id": shop.id,
                    "canteen": shop.canteen,
                    "name": shop.name,
                    "location_desc": shop.location_desc
                },
                "posts": posts_data,
                "total": len(posts_data)
            }
        })

    except Exception as e:
        print(f"获取店铺详情失败: {e}")
        return jsonify({
            "code": 500,
            "message": str(e),
            "data": None
        }), 500


MOCK_POOL = [
    {
        "canteen": "一食堂三楼",
        "shop_name": "7号窗口",
        "dish_name": "铁板黑椒鸡排饭",
        "quote": "鸡排是现煎的，黑椒汁给得足，12块钱还能加个蛋",
        "tags": ["出餐快", "量大", "便宜"],
        "score": 3
    },
    {
        "canteen": "二食堂一楼",
        "shop_name": "3号窗口",
        "dish_name": "重庆小面",
        "quote": "面条劲道，汤头很鲜，冬天来一碗整个人都暖了",
        "tags": ["便宜", "出餐快"],
        "score": 2
    },
    {
        "canteen": "三食堂二楼",
        "shop_name": "12号窗口",
        "dish_name": "自选麻辣香锅",
        "quote": "可以自己选菜，辣度自由，人均不高，味道很上头",
        "tags": ["辣", "便宜"],
        "score": 2
    },
    {
        "canteen": "一食堂二楼",
        "shop_name": "5号窗口",
        "dish_name": "照烧鸡腿饭",
        "quote": "甜咸口爱好者狂喜，鸡腿肉很嫩，分量给得实在",
        "tags": ["量大"],
        "score": 1
    },
    {
        "canteen": "二食堂三楼",
        "shop_name": "8号窗口",
        "dish_name": "番茄牛肉饭",
        "quote": "番茄味浓，牛肉片给得实在，性价比很高",
        "tags": ["便宜", "出餐快"],
        "score": 1
    },
    {
        "canteen": "三食堂一楼",
        "shop_name": "2号窗口",
        "dish_name": "烧鸭饭",
        "quote": "皮脆肉香，蘸梅子酱一绝，偶尔犒劳自己很值",
        "tags": ["量大"],
        "score": 1
    },
]


@app.route('/api/chat', methods=['POST'])
def chat():
    """AI智能推荐接口"""
    try:
        data = request.get_json()
        message = (data.get('message') or '').strip()
        if not message:
            return jsonify({"code": 400, "message": "消息不能为空"}), 400

        api_key = os.getenv("OPENAI_API_KEY")
        base_url = os.getenv("OPENAI_BASE_URL", "https://open.bigmodel.cn/api/paas/v4/")
        model = os.getenv("OPENAI_MODEL", "glm-4-flash")

        pool_desc = "\n".join([
            f"- {r['canteen']} {r['shop_name']}：{r['dish_name']}，"
            f"评价：{r['quote']}，标签：{'、'.join(r['tags'])}"
            for r in MOCK_POOL
        ])

        system_prompt = f"""你是一个校园餐厅推荐助手，根据同学的描述从以下推荐池中选出最合适的一家推荐。

推荐池：
{pool_desc}

回复格式（必须严格遵守）：
第一行：一句推荐理由，30字以内，活泼友好
第二行开始：输出一个JSON对象，格式如下：
{{
  "canteen": "食堂名",
  "shop_name": "窗口名",
  "dish_name": "菜品名",
  "quote": "评价摘录",
  "tags": ["标签1", "标签2"]
}}
只输出推荐理由和JSON，不要其他内容。"""

        reply_text = "给你推荐这个，很多同学都说不错！"
        card = None

        try:
            import openai
            client = openai.OpenAI(api_key=api_key, base_url=base_url)
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": message}
                ],
                temperature=0.7,
                timeout=10,
            )
            raw = response.choices[0].message.content.strip()
            json_match = re.search(r'\{[\s\S]+\}', raw)
            if json_match:
                reply_text = raw[:json_match.start()].strip() or reply_text
                try:
                    card = _json.loads(json_match.group())
                except Exception:
                    card = None
            else:
                reply_text = raw

        except Exception as e:
            print(f"LLM失败，降级处理: {e}")

        if not card:
            card = random.choice(MOCK_POOL)

        return jsonify({
            "code": 0,
            "data": {
                "reply": reply_text,
                "card": card
            }
        })

    except Exception as e:
        print(f"Chat error: {e}")
        return jsonify({"code": 500, "message": str(e)}), 500


@app.route('/api/actions', methods=['POST'])
def record_action():
    """记录用户行为（如"我想吃"）"""
    try:
        data = request.get_json()
        device_id = data.get('device_id')
        target_type = data.get('target_type')
        target_id = data.get('target_id')
        action_type = data.get('action_type', 'i_want_eat')

        if not all([device_id, target_type, target_id]):
            return jsonify({
                "code": 400,
                "message": "缺少必要参数",
                "data": None
            }), 400

        action = Action(
            device_id=device_id,
            target_type=target_type,
            target_id=target_id,
            action_type=action_type
        )
        db.session.add(action)
        db.session.commit()

        return jsonify({
            "code": 0,
            "message": "success",
            "data": {"action_id": action.id}
        })

    except db.exc.IntegrityError:
        db.session.rollback()
        # 唯一约束冲突，说明今天已经点过了，也算成功
        return jsonify({
            "code": 0,
            "message": "already recorded today",
            "data": None
        })
    except Exception as e:
        db.session.rollback()
        print(f"记录行为失败: {e}")
        return jsonify({
            "code": 500,
            "message": str(e),
            "data": None
        }), 500


# ========== 3D地图API ==========

@app.route('/api/map-data', methods=['GET'])
def get_map_data():
    """获取3D地图数据 - 从数据库获取或生成模拟数据"""
    try:
        shops = Shop.query.all()
        posts = Post.query.all()
        
        canteens = list(set([shop.canteen for shop in shops]))
        
        if len(shops) == 0:
            return jsonify({
                "code": 404,
                "message": "暂无数据，使用模拟数据",
                "data": None
            }), 404
        
        shop_data = []
        import random
        floors = ['一楼', '二楼', '三楼', '四楼']
        
        for idx, shop in enumerate(shops):
            shop_posts = [p for p in posts if p.shop_id == shop.id]
            latest_post = shop_posts[0] if shop_posts else None
            
            x = (idx % 7 - 3) * 10 + (random.random() - 0.5) * 5
            z = (idx // 7 - 3) * 10 + (random.random() - 0.5) * 5
            
            shop_data.append({
                "id": shop.id,
                "canteen": shop.canteen,
                "floor": floors[idx % 4],
                "name": shop.name,
                "dish_name": latest_post.dish_name if latest_post else "暂无推荐",
                "quote": latest_post.quote if latest_post else "快来评价吧",
                "tags": latest_post.tags.split(',') if latest_post and latest_post.tags else ["推荐"],
                "x": x,
                "z": z,
                "height": 5 + random.random() * 5,
                "width": 4 + random.random() * 2,
                "depth": 4 + random.random() * 2,
                "postCount": len(shop_posts)
            })
        
        return jsonify({
            "code": 0,
            "message": "success",
            "data": {
                "canteens": canteens,
                "shops": shop_data,
                "totalPosts": len(posts)
            }
        })
        
    except Exception as e:
        print(f"获取地图数据失败: {e}")
        return jsonify({
            "code": 500,
            "message": str(e),
            "data": None
        }), 500


@app.route('/map')
def map_page():
    """3D地图页面"""
    return app.send_static_file('map.html')


# ========== 错误处理 ==========

@app.errorhandler(413)
def too_large(e):
    return jsonify({
        "code": 413,
        "message": "文件太大，最大支持5MB",
        "data": None
    }), 413


# ========== 初始化数据库 ==========

def init_db():
    """初始化数据库并创建表"""
    with app.app_context():
        db.create_all()
        print("数据库初始化完成")
        seed_if_empty()


if __name__ == '__main__':
    # 初始化数据库
    init_db()

    # 启动应用
    port = int(os.getenv('PORT', 5000))
    debug = os.getenv('DEBUG', 'True').lower() == 'true'
    app.run(host='0.0.0.0', port=port, debug=debug)
