# Eingehende Anfragen aus dem Skript "postfach_zählung.py" (siehe GithubRepo Methodenbot) für mehrere Jahre zusammengestellt. Daten absteigend nach Jahren (neuestes Jahr ganz oben). Obskur i know
# immer in 12-Monaten. 1 Monat eine Zeile
x26 <- textConnection(
"1: 6
2: 1
3: 1
4: 5
5: 5
6: 2
7: 0
8: 0
9: 0
10: 0
11: 0
12: 0
1: 5
2: 4
3: 6
4: 5
5: 9
6: 8
7: 4
8: 2
9: 1
10: 5
11: 8
12: 4
1: 5
2: 2
3: 3
4: 13
5: 9
6: 10
7: 11
8: 4
9: 1
10: 4
11: 1
12: 4
1: 0
2: 0
3: 0
4: 0
5: 3
6: 8
7: 7
8: 3
9: 3
10: 4
11: 10
12: 2")

df <- read.delim(x26, header = F, sep = ":")
df$year <- factor(rep(2026:2023, each = 12))
df$month <- factor(df$V1)
df$V2[df$month %in% 7:12 & df$year == 2026] <- NA
df$V2[df$month %in% 1:4 & df$year == 2023] <- NA
sum(df$V2, na.rm = T)
library(ggplot2)
ggplot(df, aes(x = month, y = V2, color = year, group = year, fill = year)) +
  geom_area(alpha = 0.5, position = "identity", stat = "identity") +
  geom_line(alpha = 0.6) +
  theme_minimal() +
  scale_color_viridis_d(end = 0.95, guide = NULL) +
  scale_fill_viridis_d(end = 0.95) +
  labs(y = "Anzahl eingehender Anfragen", fill = "", x = "Monat")
ggsave("./grafiken/Anfrage-Anzahl.svg")
getwd()
