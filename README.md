# MEB e-Yaygın Yeni Kurs Telegram Botu

Bot iki durumda bildirim gönderir: listede daha önce görülmemiş yeni bir kurs açıldığında ve daha önce dolu (kontenjan 50/50 gibi) görünen bir kursta boş yer oluştuğunda. Mesajda kursun boş yer sayısı da yazar.

Bu bot, e-Yaygın'ın herkese açık "Açık Kurslar" sayfasını (https://e-yaygin.meb.gov.tr/pageKurslar.aspx) belirli aralıklarla tarar ve listede daha önce görmediği bir Kurs No çıktığında Telegram'dan size mesaj atar. Paylaştığınız EGT02001.aspx adresi giriş (MEBBİS/e-Devlet) istediği için kullanılmadı; açılan kurslar aynı veriyle açık sayfada listeleniyor.

## Kurulum

1. Telegram'da @BotFather'a `/newbot` yazın, adı verin ve size verdiği token'ı kopyalayın.
2. Oluşturduğunuz bota Telegram'dan bir kez "merhaba" yazın. Ardından tarayıcıda `https://api.telegram.org/bot<TOKEN>/getUpdates` adresini açın; çıktıdaki `"chat":{"id": ...}` değeri chat id'nizdir.
3. `pip install -r requirements.txt` komutunu çalıştırın (Python 3.9+).
4. `config.example.json` dosyasını `config.json` olarak kopyalayın; token, chat id ve izlemek istediğiniz il / ilçe / kurs adı değerlerini girin. İlçe ve kurs adı boş bırakılabilir; kurs adı "içerir" mantığıyla çalışır. `watches` içine istediğiniz kadar kayıt ekleyebilirsiniz.
5. Sırasıyla `python bot.py --test-telegram` (mesaj geldi mi?) ve `python bot.py --discover` (form alanları bulundu mu?) komutlarını deneyin.
6. `python bot.py` ile ilk kontrolü yapın. İlk çalıştırmada mevcut kurslar sadece kaydedilir ve "Bot başladı" mesajı gelir; sonraki çalıştırmalarda yalnızca yeni açılanlar bildirilir. Mevcut kursların da bildirilmesini istiyorsanız `--notify-existing` ekleyin.

## Sürekli çalıştırma

En basiti `python bot.py --loop 15` (15 dakikada bir). Bilgisayarınız kapalıyken de çalışmasını istiyorsanız aynı komutu bir Raspberry Pi veya Türkiye'deki bir VPS üzerinde cron ile çalıştırın: `*/15 * * * * cd /yol/meb-kurs-bot && python3 bot.py`. Windows'ta Görev Zamanlayıcı ile `python bot.py` komutunu 15 dakikada bir tetikleyebilirsiniz. GitHub Actions gibi yurt dışı sunucular kullanırsanız, kamu siteleri yurt dışı IP'lerini sık sık engellediği için bağlantı kurulamayabilir.

Çok sık sorgulamayın; 10–15 dakika aralığı hem yeterli hem siteye saygılı bir seçimdir.

## Sorun giderme

Sayfa yapısı değişirse bot "Sayfa yapısı beklenenden farklı" veya "Sonuç tablosu okunamadı" hatası verir; bu durumda `--discover` çıktısını inceleyin. İl/ilçe adı bulunamazsa hata mesajı mevcut seçenekleri listeler.
