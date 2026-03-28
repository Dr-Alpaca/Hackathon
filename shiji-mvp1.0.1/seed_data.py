"""
添加种子演示数据
运行：python seed_data.py
"""
import sys
import os

# 添加当前目录到路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import app, db, Shop, Post
from utils import extract_info


def add_seed_data():
    """添加种子演示数据"""
    with app.app_context():
        # 创建表
        db.create_all()

        # 检查是否已有数据
        existing_count = Shop.query.count()
        if existing_count > 0:
            print(f"数据库已有 {existing_count} 个店铺，跳过种子数据导入")
            return

        # 种子帖子数据
        seed_posts = [
            {
                "text": "一食堂三楼7号窗口的铁板黑椒鸡排饭真的绝了！鸡排是现煎的，黑椒汁给得足，12块钱还能加个蛋。就是排队人太多，每次都要等10分钟。",
                "device_id": "seed_device_1",
            },
            {
                "text": "二食堂一楼3号窗口重庆小面味道很正宗，面条劲道，辣椒香。出餐快，6块钱一大碗，学生党非常便宜。",
                "device_id": "seed_device_1",
            },
            {
                "text": "三食堂二楼的兰州拉面汤头很鲜，手工拉出来的面条就是筋道，牛肉片给得也不少。量很大，男生一份管饱。",
                "device_id": "seed_device_2",
            },
            {
                "text": "一食堂一楼门口的煎饼果子加肠加蛋才8块钱，便宜又好吃，出餐快，赶课的时候最喜欢去。",
                "device_id": "seed_device_2",
            },
            {
                "text": "二食堂二楼自选称重，随便拼，推荐糖醋里脊真的很不错，量大价格也不贵，量很大吃得很饱。",
                "device_id": "seed_device_3",
            },
            {
                "text": "三食堂一楼五号窗口，紫菜蛋花汤好喝又便宜，两块钱一大碗，出餐快，适合赶时间的时候来一碗。",
                "device_id": "seed_device_3",
            },
            {
                "text": "一食堂二楼五号窗口黄焖鸡米饭，可以选微辣，鸡肉嫩，土豆很入味，量够两个人吃都没问题。价格也不算贵，汤汁泡饭绝了。",
                "device_id": "seed_device_4",
            },
            {
                "text": "二食堂三楼的螺蛳粉味道真的很正宗，酸笋够味，辣度可以选，排队有点久，但是值得等。",
                "device_id": "seed_device_4",
            },
            {
                "text": "三食堂三楼的牛肉炒饭，分量特别大，男生吃不完，价格才十块，性价比很高，味道也不错，量大管饱。",
                "device_id": "seed_device_5",
            },
            {
                "text": "一食堂一楼的包子馒头，早上刚蒸出来特别香，白菜猪肉包一块五一个，便宜又好吃，出餐快。",
                "device_id": "seed_device_5",
            },
            {
                "text": "二食堂一楼沙县小吃的蒸饺很不错，皮薄馅大，蘸点醋特别好吃，蘸料自己调，出餐也快。",
                "device_id": "seed_device_6",
            },
            {
                "text": "三食堂二楼的自选菜，种类很多，可以拼，干净卫生，价格透明称重。推荐这里，口味清淡适合养生。",
                "device_id": "seed_device_6",
            },
            {
                "text": "一食堂二楼的粥铺，皮蛋瘦肉粥熬得很稠，很香，配上小菜也便宜，早上来一碗很舒服。",
                "device_id": "seed_device_7",
            },
            {
                "text": "二食堂一楼掉渣饼，加鸡柳加芝士特别香，刚做出来很酥脆，十几块钱一个，两个人分也够吃。",
                "device_id": "seed_device_7",
            },
            {
                "text": "三食堂一楼的鸭血粉丝汤，汤很鲜，鸭血给得多，粉丝可以免费续，十二块钱一碗，真的挺划算。",
                "device_id": "seed_device_8",
            },
        ]

        added_shops = {}

        for seed in seed_posts:
            # AI提取
            info = extract_info(seed['text'])
            print(f"\n提取结果: {info}")

            canteen = info.get('canteen')
            shop_name = info.get('shop_name')
            dish_name = info.get('dish_name')
            quote = info.get('quote')
            tags = info.get('tags', [])

            # 查找或创建店铺
            shop = None
            if canteen and shop_name:
                shop = Shop.query.filter_by(canteen=canteen, name=shop_name).first()
                if not shop:
                    shop = Shop(canteen=canteen, name=shop_name)
                    db.session.add(shop)
                    db.session.commit()
                    print(f"  → 创建店铺: {canteen} - {shop_name}")
                    added_shops[(canteen, shop_name)] = shop

            # 创建帖子
            post = Post(
                device_id=seed['device_id'],
                text=seed['text'],
                shop_id=shop.id if shop else None,
                dish_name=dish_name,
                quote=quote,
                tags=','.join(tags) if tags else None,
            )
            db.session.add(post)
            db.session.commit()

        print(f"\n=== 种子数据导入完成!")
        print(f"添加店铺: {len(added_shops)}")
        print(f"添加帖子: {len(seed_posts)}")


if __name__ == '__main__':
    add_seed_data()
