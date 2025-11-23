x25 <- textConnection("1: 5
2: 4
3: 6
4: 5
5: 9
6: 8
7: 4
8: 2
9: 1
10: 5
11: 6
12: 0
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

df <- read.delim(x25, header = F, sep = ":")
df$year <- factor(rep(2025:2023, each = 12))
df$month <- factor(df$V1)
df$V2[df$month == 12 & df$year == 2025] <- NA
df$V2[df$month %in% 1:4 & df$year == 2023] <- NA
library(ggplot2)
ggplot(df, aes(x = month, y = V2, color = year, group = year, fill = year)) +
  geom_area(alpha = 0.5, position = "identity", stat = "identity") +
  geom_line(alpha = 0.6) +
  theme_minimal() +
  scale_color_viridis_d(end = 0.95, guide = NULL) +
  scale_fill_viridis_d(end = 0.95) +
  labs(y = "Anzahl eingehender Anfragen", fill = "", x = "Monat")
