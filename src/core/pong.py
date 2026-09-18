"""Pong - juego clasico de dos jugadores. Generado por SovNode (skeleton local, sin LLM)."""
import pygame
import sys

pygame.init()

WIDTH, HEIGHT = 800, 480
PADDLE_W, PADDLE_H = 14, 90
BALL_SIZE = 16
PADDLE_SPEED = 6
BALL_SPEED_X, BALL_SPEED_Y = 5, 5
FPS = 60

BLACK = (12, 12, 16)
WHITE = (235, 235, 235)

screen = pygame.display.set_mode((WIDTH, HEIGHT))
pygame.display.set_caption("Pong")
clock = pygame.time.Clock()
font = pygame.font.SysFont("consolas", 36)


def reset_ball():
    ball = pygame.Rect(WIDTH // 2 - BALL_SIZE // 2, HEIGHT // 2 - BALL_SIZE // 2, BALL_SIZE, BALL_SIZE)
    vel = [BALL_SPEED_X, BALL_SPEED_Y]
    return ball, vel


def main():
    left = pygame.Rect(30, HEIGHT // 2 - PADDLE_H // 2, PADDLE_W, PADDLE_H)
    right = pygame.Rect(WIDTH - 30 - PADDLE_W, HEIGHT // 2 - PADDLE_H // 2, PADDLE_W, PADDLE_H)
    ball, ball_vel = reset_ball()
    score_left, score_right = 0, 0

    while True:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                pygame.quit()
                sys.exit()

        keys = pygame.key.get_pressed()
        if keys[pygame.K_w] and left.top > 0:
            left.y -= PADDLE_SPEED
        if keys[pygame.K_s] and left.bottom < HEIGHT:
            left.y += PADDLE_SPEED
        if keys[pygame.K_UP] and right.top > 0:
            right.y -= PADDLE_SPEED
        if keys[pygame.K_DOWN] and right.bottom < HEIGHT:
            right.y += PADDLE_SPEED

        ball.x += ball_vel[0]
        ball.y += ball_vel[1]

        if ball.top <= 0 or ball.bottom >= HEIGHT:
            ball_vel[1] *= -1

        if ball.colliderect(left) and ball_vel[0] < 0:
            ball_vel[0] *= -1
        if ball.colliderect(right) and ball_vel[0] > 0:
            ball_vel[0] *= -1

        if ball.left <= 0:
            score_right += 1
            ball, ball_vel = reset_ball()
        elif ball.right >= WIDTH:
            score_left += 1
            ball, ball_vel = reset_ball()

        screen.fill(BLACK)
        pygame.draw.aaline(screen, WHITE, (WIDTH // 2, 0), (WIDTH // 2, HEIGHT))
        pygame.draw.rect(screen, WHITE, left)
        pygame.draw.rect(screen, WHITE, right)
        pygame.draw.ellipse(screen, WHITE, ball)

        score_surf = font.render(f"{score_left}   {score_right}", True, WHITE)
        screen.blit(score_surf, (WIDTH // 2 - score_surf.get_width() // 2, 20))

        pygame.display.flip()
        clock.tick(FPS)


if __name__ == "__main__":
    main()
